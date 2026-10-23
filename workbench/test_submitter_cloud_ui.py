"""Headless GUI-boundary tests: no Tk window, process, or live network."""
import copy,json,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock,patch
from test_task_contract_v10 import load_submitter_module
from submission_transport import CloudSubmission,PendingSubmission,atomic,load_pending
class Variable:
 def __init__(self,value=None):self.value=value
 def set(self,value):self.value=value
 def get(self):return self.value
class CloudUiTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.module=load_submitter_module()
 def setUp(self):
  self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'pending.json'
  self.payload={'batchId':'fixture-original-batch','clientSubmissionId':'fixture-original-batch','runGroupId':'fixture-original-run','selectedProjects':['fixture-project'],'tasks':[{'taskId':'fixture-original-task','urls':['https://example.invalid/fixture']}],'startMode':'pending_confirmation'}
  self.fake=SimpleNamespace(project_selection_vars={'fixture-project':Variable()},info_var=Variable(),run_group_id='unrelated-run',_refresh_run_group_label=Mock(),refresh_task_box=Mock(),cloud_pending=None)
 def test_startup_restores_exact_ids_without_auto_post_or_generating_new_ids(self):
  CloudSubmission('https://service.tailexample.ts.net',self.path).prepare(self.payload)
  with patch.object(self.module,'CLOUD_PENDING_FILE',self.path),patch.object(self.module,'CloudSubmission') as transport:
   self.assertTrue(self.module.CompanyTaskSubmitter._restore_cloud_pending(self.fake));transport.assert_not_called()
  self.assertEqual('fixture-original-batch',self.fake.batch_id);self.assertEqual('fixture-original-task',self.fake.batch_tasks[0]['taskId']);self.assertTrue(self.fake.submission_locked)
  self.fake.batch_tasks[0]['taskId']='edited-display-only';self.assertEqual('fixture-original-task',self.fake.cloud_pending['payload']['tasks'][0]['taskId'])
 def test_invalid_pending_file_cannot_fall_through_to_fresh_submission(self):
  self.path.write_text('{')
  with patch.object(self.module,'CLOUD_PENDING_FILE',self.path),patch('submission_transport.time.sleep'):
   self.assertTrue(self.module.CompanyTaskSubmitter._restore_cloud_pending(self.fake))
  self.assertTrue(self.fake.cloud_pending['invalid']);self.assertTrue(self.fake.submission_locked)
 def test_pending_blocks_new_task_box_and_saved_input_reload(self):
  self.fake.cloud_pending={'schema':1}
  with patch.object(self.module.messagebox,'showinfo') as message:
   self.module.CompanyTaskSubmitter.new_task_box(self.fake);self.module.CompanyTaskSubmitter._load_saved_task_box(self.fake)
   self.assertEqual(2,message.call_count)
 def test_https_sync_does_not_probe_private_dashboard_without_login(self):
  self.fake.sender_config={'receiverUrl':'https://service.tailexample.ts.net'};self.fake.sync_info_var=Variable()
  with patch.object(self.module,'open_home_service') as transport:self.module.CompanyTaskSubmitter.sync_dashboard_status(self.fake);transport.assert_not_called()
  self.assertIn('网页登录',self.fake.sync_info_var.get());self.assertEqual('https://service.tailexample.ts.net',self.module.CompanyTaskSubmitter._dashboard_url(self.fake))
 def test_cloud_success_saves_receipt_before_removing_resume_state(self):
  events=[];self.fake.query_cloud_button=Mock();self.fake._accept_submission=lambda *args:events.append('receipt')
  transport=SimpleNamespace(complete=lambda:events.append('remove-pending'))
  self.module.CompanyTaskSubmitter._cloud_success(self.fake,transport,self.payload,{'ok':True})
  self.assertEqual(['receipt','remove-pending'],events);self.assertIsNone(self.fake.cloud_pending)

 def test_releasing_terminal_receipt_requires_explicit_new_box_for_new_intent(self):
  self.fake.query_cloud_button=Mock();self.fake.cloud_pending={'schema':1}
  self.module.CompanyTaskSubmitter._cloud_release_done(self.fake)
  self.assertIsNone(self.fake.cloud_pending);self.assertTrue(self.fake.submission_locked)
  self.assertIn('新建任务箱',self.fake.info_var.get())

class QueryResumeBoundaryTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  import ast
  cls.methods=[]
  root=Path(__file__).resolve().parents[1]/'portable_submitter'
  for path,class_name,query_name in ((root/'正式任务递交器/company_task_submitter.pyw','CompanyTaskSubmitter','_query_cloud_receipt'),(root/'ExampleBrand自然流任务递交器/ExampleBrand自然流任务投递器.pyw','ExamplebrandSubmitter','_query_receipt')):
   tree=ast.parse(path.read_text(encoding='utf-8'));body=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==class_name).body
   scope={'messagebox':SimpleNamespace(showinfo=Mock(),askyesno=Mock(return_value=True))}
   for name in (query_name,'_resume_cloud_submission'):
    node=next(n for n in body if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),scope)
   cls.methods.append((class_name,query_name,scope))
 def fake(self):
  return SimpleNamespace(cloud_pending={'origin':'https://service.tailexample.ts.net','payload':{'batchId':'original'}},cloud_worker_running=False,submit_in_progress=False,query_in_progress=False,_submit_cloud=Mock())
 def test_both_query_buttons_explicitly_select_query_only(self):
  for name,query,scope in self.methods:
   with self.subTest(name=name):
    fake=self.fake();scope[query](fake)
    fake._submit_cloud.assert_called_once_with(fake.cloud_pending['origin'],fake.cloud_pending['payload'],query_only=True)
 def test_both_resume_buttons_require_confirmation_and_keep_original_payload(self):
  for name,query,scope in self.methods:
   with self.subTest(name=name):
    fake=self.fake();scope['messagebox'].askyesno.return_value=False
    scope['_resume_cloud_submission'](fake);fake._submit_cloud.assert_not_called()
    scope['messagebox'].askyesno.return_value=True;scope['_resume_cloud_submission'](fake)
    fake._submit_cloud.assert_called_once_with(fake.cloud_pending['origin'],fake.cloud_pending['payload'])
 def test_busy_or_invalid_resume_never_starts_another_worker(self):
  for name,query,scope in self.methods:
   for attr in ('busy','invalid','absent'):
    with self.subTest(name=name,state=attr):
     fake=self.fake()
     if attr=='busy':fake.cloud_worker_running=True;fake.submit_in_progress=True
     elif attr=='invalid':fake.cloud_pending={'invalid':True}
     else:fake.cloud_pending=None
     scope['_resume_cloud_submission'](fake);fake._submit_cloud.assert_not_called()
 def test_transport_copies_are_identical(self):
  root=Path(__file__).resolve().parents[1];canonical=(root/'workbench/submission_transport.py').read_bytes()
  paths=list((root/'portable_submitter').glob('*/submission_transport.py'));self.assertEqual(2,len(paths))
  for path in paths:self.assertEqual(canonical,path.read_bytes())
