"""Office-baseline V14 boundaries without Tk windows or live HTTP."""
import importlib.machinery,importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]/'portable_submitter'
class Var:
 def __init__(self,v=None):self.value=v
 def get(self):return self.value
 def set(self,v):self.value=v
def load(path,name):
 for key in ('submission_transport','submitter_connection','submitter_instance_lock','local_import_bridge','task_inbox','task_contract','examplebrand_contract'):
  sys.modules.pop(key,None)
 sys.path.insert(0,str(path.parent))
 try:
  loader=importlib.machinery.SourceFileLoader(name,str(path));spec=importlib.util.spec_from_loader(name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m);return m
 finally:sys.path.pop(0)
class PortableTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.p=Path(self.temp.name);self.env=patch.dict(os.environ,LOCALAPPDATA=str(self.p),APPDATA=str(self.p));self.env.start();self.addCleanup(self.env.stop)
 def test_company_mutex_and_all_child_exe_paths_preserved(self):
  m=load(ROOT/'unified_task_submitter.pyw','v14_unified')
  fake=SimpleNamespace(package_root=self.p)
  self.assertEqual('正式任务递交器/程序/AI-Video-Task-Submitter/AI-Video-Task-Submitter.exe',m.UnifiedTaskSubmitter._child_paths(fake,'formal')[0].relative_to(self.p).as_posix())
  self.assertEqual('ExampleBrand自然流任务递交器/程序/ExampleBrand-Natural-Flow-Task-Submitter/ExampleBrand-Natural-Flow-Task-Submitter.exe',m.UnifiedTaskSubmitter._child_paths(fake,'examplebrand')[0].relative_to(self.p).as_posix())
  source=(ROOT/'unified_task_submitter.pyw').read_text('utf-8');self.assertIn('acquire_instance_lock("AI-Video-Task-Submitter-Unified")',source);self.assertIn('V14 · 统一入口',source)
 def test_endpoint_override_preserves_all_configuration_bytes_and_custom_fields(self):
  m=load(ROOT/'submitter_connection.py','v14_connection')
  config=self.p/'task_submitter_config.json';raw=json.dumps({'receiverUrl':'http://fixture.invalid','accessToken':'fixture-private-canary','customField':'preserve','runGroupId':'run-original'}).encode();config.write_bytes(raw)
  result,source=m.load_connection(self.p,config)
  self.assertEqual(raw,config.read_bytes());self.assertEqual('preserve',result['customField']);self.assertEqual('',result['accessToken']);self.assertEqual('https://service.tailexample.ts.net',result['receiverUrl']);self.assertEqual('',m.connection_error(result));self.assertFalse((self.p/'AI-Video-Task-Submitter/V13/connection.json').exists())
 def test_examplebrand_reload_restores_original_ids_without_post(self):
  m=load(ROOT/'ExampleBrand自然流任务递交器/ExampleBrand自然流任务投递器.pyw','v14_examplebrand')
  payload={'batchId':'examplebrand-original','runGroupId':'run-original','productionDate':'10.19','tasks':[{'taskId':'original-task','product':'清淤泵','expectedCount':1,'urls':['https://example.invalid']} ]}
  path=self.p/'pending.json'
  import submission_transport
  submission_transport.CloudSubmission('https://service.tailexample.ts.net',path).prepare(payload)
  fake=SimpleNamespace(run_group_var=Var(),date_var=Var(),status_var=Var(),refresh_task_box=Mock())
  with patch.object(m,'CLOUD_PENDING_FILE',path),patch.object(m,'CloudSubmission') as controller:m.ExamplebrandSubmitter._restore_cloud_pending(fake);controller.assert_not_called()
  self.assertEqual('examplebrand-original',fake.batch_id);self.assertEqual('original-task',fake.rows[0]['taskId']);self.assertTrue(fake.submission_locked)
  with patch.object(m.messagebox,'showinfo') as message:m.ExamplebrandSubmitter.new_task_box(fake);m.ExamplebrandSubmitter.reload_saved_task_box(fake);self.assertEqual(2,message.call_count)
 def test_examplebrand_receipt_persisted_before_pending_removed(self):
  m=load(ROOT/'ExampleBrand自然流任务递交器/ExampleBrand自然流任务投递器.pyw','v14_examplebrand_receipt');events=[]
  fake=SimpleNamespace(_closing=False,current_operation_id=3,_persist_submission=lambda *a,**kw:events.append('receipt') or True,_save_task_box=lambda:events.append('saved') or True,refresh_task_box=Mock(),status_var=Var())
  controller=SimpleNamespace(complete=lambda:events.append('remove'))
  m.ExamplebrandSubmitter._cloud_accepted(fake,3,{'batchId':'fixture','runGroupId':'run-fixture'},{'ok':True},controller)
  self.assertEqual(['receipt','saved','remove'],events);self.assertTrue(fake.submission_locked);self.assertIsNone(fake.cloud_pending)
 def test_examplebrand_failed_persistence_keeps_pending(self):
  m=load(ROOT/'ExampleBrand自然流任务递交器/ExampleBrand自然流任务投递器.pyw','v14_examplebrand_failed');controller=Mock()
  fake=SimpleNamespace(_closing=False,current_operation_id=3,_persist_submission=lambda *a,**k:False,_cloud_failed=Mock())
  m.ExamplebrandSubmitter._cloud_accepted(fake,3,{'batchId':'fixture','runGroupId':'run-fixture'},{'ok':True},controller)
  controller.complete.assert_not_called();fake._cloud_failed.assert_called_once()
 def test_examplebrand_uses_batch_id_when_contract_has_no_client_submission_id(self):
  m=load(ROOT/'ExampleBrand自然流任务递交器/submission_transport.py','v14_transport')
  class Reply:
   code=200
   def __init__(self,body):self.body=json.dumps(body).encode()
   def read(self,n):return self.body[:n]
   def __enter__(self):return self
   def __exit__(self,*args):pass
  transport=Mock();transport.open.side_effect=[Reply({}),Reply({'ok':True}),Reply({'ok':True})]
  controller=m.CloudSubmission('https://service.tailexample.ts.net',self.p/'pending.json',transport)
  self.assertTrue(controller.run({'batchId':'examplebrand-original','tasks':[{'taskId':'task-original'}]})['ok'])
  self.assertEqual(2,sum(call.args[0].method=='POST' for call in transport.open.call_args_list))
 def test_formal_local_import_blocks_when_cloud_receipt_pending(self):
  m=load(ROOT/'正式任务递交器/company_task_submitter.pyw','v14_formal')
  fake=SimpleNamespace(cloud_pending={'schema':1},_set_bridge_child_status=Mock())
  m.CompanyTaskSubmitter._apply_local_import(fake,'import-fixture',{})
  fake._set_bridge_child_status.assert_called_once()
if __name__=='__main__':unittest.main(verbosity=2)
