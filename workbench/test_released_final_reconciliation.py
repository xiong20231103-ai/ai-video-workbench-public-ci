import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from resume_plan import released_final_is_verified, resolve_resume_plan
from capcut_handoff import CapCutHandoffStore


class ReleasedFinalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'finished.mp4'
        self.output.write_bytes(b'known-final-' * 200)
        self.report = self.root / 'qc.json'
        self.qc = {'status': 'passed', 'taskId': 'done', 'canonicalTaskId': 'parent',
                   'fileName': self.output.name, 'fileSize': self.output.stat().st_size,
                   'sha256': hashlib.sha256(self.output.read_bytes()).hexdigest()}
        self.report.write_text(json.dumps(self.qc))
        self.row = {'taskId': 'done', 'remotePipelineTaskId': 'parent',
                    'project': 'project', 'material': 'material', 'productionDate': '10.15',
                    'productionStatus': 'completed', 'status': 'failed', 'qualityStatus': 'passed',
                    'draftReleaseStatus': 'released', 'draftCleanupStatus': 'pending',
                    'finalOutputPath': str(self.output), 'qualityReportPath': str(self.report),
                    'error': 'CAPCUT_DRAFT_UNEXPECTED_SEGMENT'}
        self.task = {'taskId': 'parent', 'runGroupId': 'run', 'batchId': 'batch',
                     'project': 'project', 'material': 'material', 'productionDate': '10.15',
                     'expectedCount': 2}
        self.pending = {**self.row, 'taskId': 'next', 'status': 'waiting_client',
                        'productionStatus': 'pending', 'qualityStatus': '',
                        'videoPath': str(self.output), 'audioPath': str(self.output)}

    def plan(self):
        return resolve_resume_plan(self.task, [self.row, self.pending],
            preferred_child_task_id='done', verify_final=lambda r: released_final_is_verified(r))

    def test_release_failure_preserves_final_and_selects_only_remaining_child(self):
        plan = self.plan()
        self.assertEqual('ready', plan['decision'])
        self.assertEqual('next', plan['childTaskId'])
        self.assertEqual(['done'], plan['verifiedFinals'])
        self.assertEqual(['next'], [x['row']['taskId'] for x in plan['evidence']])

    def test_report_identity_hash_size_and_pending_lease_fail_closed(self):
        for field, value in [('taskId', 'foreign'), ('canonicalTaskId', 'foreign'),
                             ('sha256', '0' * 64), ('fileSize', 1),
                             ('fileName', 'other.mp4'), ('status', 'failed')]:
            with self.subTest(field=field):
                self.report.write_text(json.dumps({**self.qc, field: value}))
                self.assertFalse(released_final_is_verified(self.row))
                self.assertEqual('blocked', self.plan()['decision'])
        self.report.write_text(json.dumps(self.qc))
        for field, value in [('draftReleaseStatus', 'pending'),
                             ('draftCleanupStatus', 'release_pending'),
                             ('qualityStatus', 'failed')]:
            with self.subTest(field=field):
                self.assertFalse(released_final_is_verified({**self.row, field: value}))

    def test_changed_or_missing_final_and_malformed_report_are_rejected(self):
        self.output.write_bytes(b'corrupted---' * 200)
        self.assertFalse(released_final_is_verified(self.row))
        self.output.unlink()
        self.assertFalse(released_final_is_verified(self.row))
        self.output.write_bytes(b'known-final-' * 200)
        for invalid in ('{', '[]', 'null'):
            self.report.write_text(invalid)
            self.assertFalse(released_final_is_verified(self.row))

    def test_handoff_projection_keeps_error_and_does_not_write_or_select_completed(self):
        store = CapCutHandoffStore(self.root / 'handoff', self.root / 'out')
        store.save_date('10.15', {'tasks': [self.row, self.pending]})
        before = store.date_file('10.15').read_bytes()
        rows = store.load_date('10.15')['tasks']
        self.assertEqual('completed', rows[0]['status'])
        self.assertFalse(rows[0]['selected'])
        self.assertEqual('failed', rows[0]['maintenanceStatus'])
        self.assertEqual(self.row['error'], rows[0]['maintenanceError'])
        self.assertEqual(before, store.date_file('10.15').read_bytes())
        self.assertEqual('waiting_client', rows[1]['status'])

    def test_workbench_launch_selection_never_reedits_reconciled_final(self):
        from workbench import Workbench
        from types import SimpleNamespace
        store = CapCutHandoffStore(self.root / 'handoff', self.root / 'out')
        done = {**self.row, 'generationRecord': 'done-record'}
        pending = {**self.pending, 'generationRecord': 'next-record'}
        store.save_date('10.15', {'tasks': [done, pending]})
        app = object.__new__(Workbench)
        app.active_external_task = {**self.task, 'date': '10.15'}
        app.capcut_store = store
        app._is_remote_auto_sequence_task = lambda task: True
        app._release_capcut_launch_guard = lambda task_id: True
        app._continue_command_has_launched_capcut = lambda task_id: False
        app._capcut_status_file_is_running = lambda path: False
        app._mark_continue_capcut_recovery_started = lambda task_id: None
        calls = []
        app._start_remote_capcut = lambda *args, **kwargs: calls.append((args, kwargs)) or True
        self.assertTrue(app._resume_remote_capcut_if_ready(recovery=True))
        self.assertEqual(1, len(calls))
        self.assertEqual({'next'}, calls[0][0][2])
        self.assertFalse(calls[0][1].get('release_only', False))

    def test_wrong_parent_or_count_still_blocks(self):
        self.pending['remotePipelineTaskId'] = 'other'
        self.assertEqual('blocked', self.plan()['decision'])


if __name__ == '__main__':
    unittest.main()
