"""Temp-only GUI-free entry tests; no Workbench import, production or subprocess."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from voice_dependency_preflight import check_voice_dependencies, dependency_message

ROOT = Path(__file__).resolve().parent


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='voice-probe-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root/'src').mkdir()
        (self.root/'src/dependencyPreflight.js').write_text('// inert fixture')
    def reply(self, body, code=0):
        return SimpleNamespace(stdout=json.dumps(body).encode(), returncode=code)
    def test_healthy_and_missing_contract(self):
        for body, code in (({'ok': True, 'code': '', 'missing': []}, 0),
                ({'ok': False, 'code': 'VOICE_DEPENDENCIES_MISSING', 'missing': ['dotenv']}, 1)):
            with patch('voice_dependency_preflight.subprocess.run', return_value=self.reply(body, code)) as run:
                self.assertEqual(body, check_voice_dependencies(self.root))
                self.assertFalse(run.call_args.kwargs['shell']); self.assertEqual(10, run.call_args.kwargs['timeout'])
    def test_timeout_errors_malformed_and_wrong_exit_fail_closed_without_values(self):
        bad = [self.reply({'ok': True, 'code': '', 'missing': []}, 1),
               self.reply({'ok': False, 'code': 'VOICE_DEPENDENCIES_MISSING', 'missing': ['inert-invalid-name']}, 1),
               SimpleNamespace(stdout=b'not-json', returncode=0)]
        for result in bad:
            with patch('voice_dependency_preflight.subprocess.run', return_value=result):
                self.assertEqual('VOICE_DEPENDENCY_CHECK_FAILED', check_voice_dependencies(self.root)['code'])
        for error in (OSError('inert-not-for-output'), subprocess.TimeoutExpired('inert', 10)):
            with patch('voice_dependency_preflight.subprocess.run', side_effect=error):
                self.assertEqual('VOICE_DEPENDENCY_CHECK_FAILED', check_voice_dependencies(self.root)['code'])


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='voice-entry-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root/'client.exe').touch()
        tree = ast.parse((ROOT/'workbench.py').read_text(encoding='utf-8'))
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Workbench')
        nodes = [node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name in {'launch_voice_client', '_voice_ops_status'}]
        self.namespace = dict(Path=Path, dependency_message=dependency_message, check_voice_dependencies=Mock(),
            canonical_task_id=lambda task: task.get('taskId', ''), load_current_context=lambda: {},
            contract_language_plan=lambda task: [], VOICE_LAUNCH_FAILED='failed')
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<isolated-workbench-voice-entry>', 'exec'), self.namespace)
        self.identity = {'canonicalTaskId': 'task-inert', 'batchId': 'batch-inert'}
        self.model = SimpleNamespace(active_external_task={'taskId': 'task-inert'},
            _voice_client_path=lambda: self.root/'client.exe', _voice_task_identity=lambda _: self.identity,
            _voice_launch_result_for_existing_process=Mock(return_value=None),
            _persist_voice_launch_state=Mock(), _set_remote_pipeline_status=Mock(),
            status_var=SimpleNamespace(set=Mock()), voice_info_var=SimpleNamespace(set=Mock()), write_log=Mock(),
            _sync_voice_output_dirs=Mock(side_effect=RuntimeError('healthy_original_path_reached')))
    def test_both_modes_block_before_settings_plan_or_browser_and_publish_alarm(self):
        for unattended in (True, False):
            result = {'ok': False, 'code': 'VOICE_DEPENDENCIES_MISSING', 'missing': ['csv-parse']}
            self.namespace['check_voice_dependencies'].return_value = result
            self.assertEqual('failed', self.namespace['launch_voice_client'](self.model, unattended=unattended))
            self.model._sync_voice_output_dirs.assert_not_called()
            self.model._set_remote_pipeline_status.assert_called_with('task-inert', 'blocked_voice', dependency_message(result), error_code=result['code'])
            self.assertEqual(result['code'], self.model.last_voice_launch_error['errorCode'])
    def test_healthy_preserves_original_path_and_existing_voice_is_not_interrupted(self):
        self.namespace['check_voice_dependencies'].return_value = {'ok': True, 'code': '', 'missing': []}
        with self.assertRaisesRegex(RuntimeError, 'healthy_original_path_reached'):
            self.namespace['launch_voice_client'](self.model, unattended=True)
        self.model._voice_launch_result_for_existing_process.return_value = 'already-running'
        self.namespace['check_voice_dependencies'].reset_mock()
        self.assertEqual('already-running', self.namespace['launch_voice_client'](self.model, unattended=True))
        self.namespace['check_voice_dependencies'].assert_not_called()
    def test_current_dependency_failure_is_visible_even_if_old_handoff_exists(self):
        alarm = {**self.identity, 'launchState': 'failed', 'errorCode': 'VOICE_DEPENDENCIES_MISSING', 'message': '配音缺依赖，未开始'}
        self.model._read_local_json_object = lambda _: alarm
        self.model._voice_launch_state_file = lambda: self.root/'launch.json'
        self.model._voice_identity_matches = lambda left, right: left.get('canonicalTaskId') == right.get('canonicalTaskId')
        self.model._voice_handoff_file = Mock(side_effect=AssertionError('must not hide alarm behind old success'))
        result = self.namespace['_voice_ops_status'](self.model)
        self.assertEqual('failed', result['state']); self.assertEqual(1, result['failed'])
        self.assertEqual(alarm['errorCode'], result['errorCode'])
        self.model._voice_handoff_file.assert_not_called()
    def test_batch_implementation_is_exact_original_except_filename(self):
        # Guard the mechanical move: implementation retains the reviewed logic.
        self.assertIn("await import('./runBatchImpl.js')", (ROOT.parent/'voice-client/src/runBatch.js').read_text(encoding='utf-8'))
        original_hash = '014ea6546edfeeb85ad86ea38f884b9352be65472aec24902887de3d64f5e76e'
        implementation = (ROOT.parent/'voice-client/src/runBatchImpl.js').read_text(encoding='utf-8')
        # Recompose the purely extracted retry helper and retain the ORIGINAL
        # golden hash. Any change to its reviewed logic still fails this gate.
        prefix = "import { loadRetryCompletedItems } from './retryCompletedItems.js';\n"
        self.assertTrue(implementation.startswith(prefix))
        implementation = implementation[len(prefix):].replace(
            'import {\n  persistVerifiedCompletionLedger',
            'import {\n  loadVerifiedCompletionLedger,\n  persistVerifiedCompletionLedger')
        helper = (ROOT.parent/'voice-client/src/retryCompletedItems.js').read_text(encoding='utf-8')
        expected_header = "import fs from 'node:fs/promises';\nimport { loadVerifiedCompletionLedger } from './verifiedCompletionLedger.js';\n\nexport "
        self.assertTrue(helper.startswith(expected_header))
        helper = helper[len(expected_header):].rstrip('\n')
        implementation = implementation.replace('\nasync function writeBatchHandoff',
            helper + '\n\nasync function writeBatchHandoff', 1).encode('utf-8')
        self.assertEqual(original_hash, hashlib.sha256(implementation).hexdigest())

if __name__ == '__main__': unittest.main()
