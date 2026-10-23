import ast
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fish_reconciliation import observe_failed_fish, process_absent
from recovery_control import RecoveryControl

class FishLifecycleTests(unittest.TestCase):
    def facts(self):
        c=dict(commandId='c',taskId='t',step='step3',runGroupId='r',batchId='b',claimedAt='2026-10-21T03:16:35+00:00',state='reconciliation_required',stableRecoveryKey='key')
        m=dict(commandId='c',taskId='t',runGroupId='r',batchId='b',bridgePid=12)
        tasks=['lang'+str(i) for i in range(9)]
        rt=dict(pid=12,batchId='fish',canonicalTaskIds=['t'],taskIds=tasks,state='terminal',terminalStatus='completed_with_errors',startedAt='2026-10-21T03:16:40Z')
        h=dict(canonicalTaskId='t',runGroupId='r',sourceBatchId='b',batchId='fish',taskIds=tasks,status='completed_with_errors',completedAt='2026-10-21T03:24:50Z',total=9,completed=8,failed=1,completedItems=[dict(taskId=t) for t in tasks[:8]],failedItems=[dict(taskId=tasks[8])])
        return c,m,rt,h

    def test_partial_terminal_closes_only_matching_command_without_dispatch(self):
        c,m,rt,h=self.facts()
        result=observe_failed_fish(c,m,rt,h,absent=lambda pid:True)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['failedTaskIds'],['lang8'])
        self.assertEqual(len(result['completedTaskIds']),8)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';mail=Path(d)/'mail.json'
            p.write_text(json.dumps(dict(schemaVersion=4,manualPause=True,commands={'c':c},entries={'key':dict(activeCommandId='c',automaticAttemptCount=1)},history=[])),encoding='utf-8')
            mail.write_text(json.dumps(m),encoding='utf-8');before=mail.read_bytes()
            ctrl=RecoveryControl(p,Path(d)/'lock',mail)
            ctrl.reconcile_pending_commands(lambda command:result,allow_redispatch=False)
            state=ctrl.status()
            self.assertEqual(state['commands']['c']['state'],'failed')
            self.assertEqual(state['entries']['key']['activeCommandId'],'')
            self.assertEqual(state['entries']['key']['automaticAttemptCount'],1)
            self.assertTrue(state['manualPause'])
            self.assertEqual(mail.read_bytes(),before)

    def test_partial_conflicts_or_live_producer_stay_unknown(self):
        c,m,rt,h=self.facts()
        self.assertEqual(observe_failed_fish(c,m,rt,h,absent=lambda pid:False)['status'],'unknown')
        for field,value in [('failed',0),('completed',9),('total',10),('failedItems',[{'taskId':'lang0'}]),('completedItems',[]),('sourceBatchId','other')]:
            bad=copy.deepcopy(h);bad[field]=value
            self.assertEqual(observe_failed_fish(c,m,rt,bad,absent=lambda pid:True)['status'],'unknown',field)
        rt['terminalStatus']='failed'
        self.assertEqual(observe_failed_fish(c,m,rt,h,absent=lambda pid:True)['status'],'unknown')

    @unittest.skipUnless(os.name=='nt','Windows native process handles')
    def test_real_exit_handle_is_not_a_live_producer(self):
        p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.assertFalse(process_absent(p.pid))
            p.terminate();p.wait(timeout=10)
            # Popen still owns a valid process handle here (the actual incident).
            self.assertTrue(process_absent(p.pid))
        finally:
            if p.poll() is None:p.kill();p.wait()

    def test_access_denied_and_failed_native_queries_are_unknown(self):
        class API:
            class Call:
                def __init__(self,result):self.result=result
                def __call__(self,*args):return self.result
            def __init__(self,opened):
                self.OpenProcess=self.Call(opened);self.CloseHandle=self.Call(1)
                self.GetExitCodeProcess=self.Call(0);self.WaitForSingleObject=self.Call(0xffffffff)
        import fish_reconciliation as f
        with patch.object(f.ctypes,'WinDLL',return_value=API(0),create=True),patch.object(f.ctypes,'get_last_error',return_value=5,create=True):
            self.assertFalse(process_absent(12))
        with patch.object(f.ctypes,'WinDLL',return_value=API(1),create=True):
            self.assertFalse(process_absent(12))

    def test_workbench_binds_only_actual_live_voice_producer(self):
        tree=ast.parse(Path('workbench.py').read_text(encoding='utf-8-sig'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Workbench')
        names={'_live_voice_launch_identity','_recovery_execution_receipt','_recovery_process_observation'}
        methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in names]
        env={'capture_process_identity':lambda pid,role:dict(pid=pid,role=role),'observe_process_identity':lambda i:'matched_alive','CONTINUE_COMMAND_FILE':'mail'}
        exec(compile(ast.Module(body=methods,type_ignores=[]),'<fixture>','exec'),env)
        owner=SimpleNamespace(voice_launch_identity={'canonicalTaskId':'t'},voice_batch_process=SimpleNamespace(pid=12,poll=lambda:None))
        owner._live_voice_launch_identity=lambda task:env['_live_voice_launch_identity'](owner,task)
        owner._read_local_json_object=lambda p:{'taskId':'t'}
        receipt=env['_recovery_execution_receipt'](owner,'t')
        self.assertEqual(receipt['processIdentity']['pid'],12)
        self.assertEqual(receipt['producer'],'fish')
        observed=env['_recovery_process_observation'](owner,{'step':'step3','taskId':'t','commandId':'c'})
        self.assertEqual(observed['status'],'alive')
        self.assertEqual(owner._live_voice_launch_identity('other'),{})
        owner.voice_batch_process.poll=lambda:1
        self.assertEqual(owner._live_voice_launch_identity('t'),{})

if __name__=='__main__':unittest.main()
