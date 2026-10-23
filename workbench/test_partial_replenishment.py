"""Synthetic receiver and parent-progress tests. No production URLs or clients."""
import asyncio,copy,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from fastapi import HTTPException
import task_receiver as receiver
import workbench as wb

class PartialReplenishmentTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.pending=self.root/'pending';self.imported=self.root/'imported';self.pending.mkdir();self.imported.mkdir()
  self.record={'taskId':'fixture-parent','expectedCount':21,'completed':16,'missingCount':5,'status':'blocked_missing_links','project':'ExampleBrand','material':'MaterialPump','date':'fixture-date','runGroupId':'fixture-run','batchId':'fixture-batch'}
  self.serial=0;self.writes=[]
 def create(self,template,urls,**kwargs):
  self.serial+=1
  # Use the real normalizer; task-template side effects are not needed here.
  from task_inbox import normalize_urls
  return {'taskId':'fixture-child-'+str(self.serial),'urls':normalize_urls(urls),'expectedCount':21,'project':'ExampleBrand','material':'MaterialPump'}
 def submit(self,task,*,start):
  task=copy.deepcopy(task);task.update(batchId='fixture-transport-'+task['taskId'],status='pending_confirmation')
  (self.pending/(task['taskId']+'.json')).write_text(json.dumps(task),encoding='utf-8');self.writes.append((task,start))
  return {'ok':True,'taskId':task['taskId'],'batchId':task['batchId']}
 def call(self,urls,start=True):
  request=SimpleNamespace(json=Mock())
  async def body():return {'urls':urls,'startNow':start}
  request.json=body
  with patch.object(receiver,'INBOX_DIR',self.pending),patch.object(receiver,'IMPORTED_DIR',self.imported),patch.object(receiver,'_require_sender_permission'),patch.object(receiver,'load_project_download_statuses',return_value={'items':{'fixture-parent':self.record}}),patch.object(receiver,'load_templates',return_value={'templates':[{'id':'fixture-template','project':'ExampleBrand'}]}),patch.object(receiver,'create_task',side_effect=self.create),patch.object(receiver,'_submit_dashboard_task',side_effect=self.submit):
   # request.json is an immediately completed fixture; drive the coroutine
   # without constructing Windows asyncio's socketpair or opening a listener.
   operation=receiver.replenish_dashboard_task('fixture-parent',request)
   try:operation.send(None)
   except StopIteration as result:return result.value
   finally:operation.close()
   raise AssertionError('unexpected asynchronous I/O in isolated endpoint')
 def urls(self,n,offset=0):return ['https://example.test/video/'+str(i+offset) for i in range(n)]
 def finish(self,count):
  task=self.writes[-1][0];path=self.pending/(task['taskId']+'.json');task['status']='merged_into_parent';(self.imported/path.name).write_text(json.dumps(task),encoding='utf-8');path.unlink();self.record.update(completed=self.record['completed']+count,missingCount=self.record['missingCount']-count,lastReplenishmentTaskId=task['taskId'])
 def test_two_then_three_preserve_original_total_and_identity(self):
  self.call('\n'.join(self.urls(2)));first=self.writes[0][0]
  self.assertEqual((2,21,16,5),(first['expectedCount'],first['replenishmentBaseExpected'],first['replenishmentCompletedBefore'],first['replenishmentMissingBefore']))
  self.assertEqual(('fixture-parent','fixture-parent','fixture-run','fixture-batch'),(first['replenishForTaskId'],first['canonicalTaskId'],first['runGroupId'],first['replenishmentParentBatchId']))
  self.finish(2);self.call(self.urls(3,2),False);last=self.writes[-1][0]
  self.assertEqual((3,21,18,3),(last['expectedCount'],last['replenishmentBaseExpected'],last['replenishmentCompletedBefore'],last['replenishmentMissingBefore']));self.assertFalse(self.writes[-1][1]);self.assertEqual(21,self.record['expectedCount'])
 def test_zero_or_above_gap_rejected_without_submit(self):
  for urls in [[],self.urls(6)]:
   with self.assertRaises(HTTPException) as error:self.call(urls)
   self.assertEqual(400,error.exception.status_code)
  self.assertEqual([],self.writes)
 def test_full_gap_remains_compatible(self):
  self.call(self.urls(5));self.assertEqual(5,self.writes[0][0]['expectedCount']);self.assertTrue(self.writes[0][1])
 def test_exact_replay_even_after_receiver_restart_never_submits_twice(self):
  first=self.call(self.urls(2));self.serial=0;again=self.call(list(reversed(self.urls(2))))
  self.assertTrue(again['duplicate']);self.assertEqual(first['taskId'],again['taskId']);self.assertFalse(again['workbenchLaunchRequested']);self.assertEqual(1,len(self.writes))
 def test_overlapping_links_and_parallel_pending_child_rejected(self):
  self.call(self.urls(2))
  for urls in [self.urls(2,1),self.urls(2,10)]:
   with self.assertRaises(HTTPException):self.call(urls)
  self.assertEqual(1,len(self.writes))
 def test_partial_success_leaves_real_remaining_gap(self):
  self.call(self.urls(2));self.finish(1);self.call(self.urls(4,10));self.assertEqual(17,self.writes[-1][0]['replenishmentCompletedBefore']);self.assertEqual(4,self.writes[-1][0]['expectedCount'])
 def test_completed_gap_still_recognizes_original_duplicate(self):
  original=self.call(self.urls(5));self.finish(5);again=self.call(self.urls(5));self.assertTrue(again['duplicate']);self.assertEqual(original['taskId'],again['taskId']);self.assertEqual(1,len(self.writes))
 def test_real_task_builder_preserves_partial_normalized_links(self):
  from task_inbox import create_task
  catalog={'templateVersion':'fixture'};template={'id':'fixture-template','project':'ExampleBrand','material':'MaterialPump','materials':['MaterialPump'],'expectedCount':21}
  def real_create(*args,**kwargs):
   with patch('task_inbox.template_by_id',return_value=(catalog,template)):return create_task(*args,**kwargs)
  with patch.object(self,'create',side_effect=real_create):self.call('\n'.join(self.urls(2)))
  self.assertEqual(self.urls(2),self.writes[0][0]['urls']);self.assertEqual(2,self.writes[0][0]['expectedCount'])
 def test_failed_start_response_keeps_persisted_child_and_prevents_resend(self):
  original=self.submit
  def failed_start(task,*,start):original(task,start=start);raise OSError('fixture startup unavailable')
  with patch.object(self,'submit',side_effect=failed_start):
   with self.assertRaises(HTTPException):self.call(self.urls(2))
  again=self.call(self.urls(2));self.assertTrue(again['duplicate']);self.assertEqual(1,len(self.writes))
 def test_running_complete_and_inconsistent_parent_rejected(self):
  for change in [{'status':'running'},{'missingCount':0},{'completed':20,'missingCount':5}]:
   original=dict(self.record);self.record.update(change)
   with self.assertRaises(HTTPException):self.call(self.urls(1))
   self.record=original
  self.assertEqual([],self.writes)

class ParentProgressTests(unittest.TestCase):
 def shell(self,base=16,success=2):
  shell=SimpleNamespace(active_external_task={'taskId':'fixture-child','replenishForTaskId':'fixture-parent','replenishmentBaseExpected':21,'replenishmentCompletedBefore':base},success_var=Mock(get=lambda:str(success)),failed_var=Mock(get=lambda:'0'),_integer=lambda x,default=0:int(x) if x not in (None,'') else default,_expected_download_count=lambda:2,_download_failure_items=lambda:[],live_tasks={},last_download_runtime_failure=None,write_log=Mock())
  return shell
 def save(self,shell,parent,phase):
  with patch.object(wb,'load_current_context',return_value={}),patch.object(wb,'load_project_download_statuses',return_value={'items':{'fixture-parent':parent}}),patch.object(wb,'completed_download_checkpoint_count',return_value=int(shell.success_var.get())),patch.object(wb,'save_project_download_status') as save:
   wb.Workbench.save_project_download_progress(shell,phase);return save.call_args.args[0]
 def test_partial_finish_and_repeated_restore_count_once(self):
  parent={'taskId':'fixture-parent','expectedCount':21,'completed':16,'missingCount':5}
  shell=self.shell();first=self.save(shell,parent,'finished');second=self.save(shell,first,'finished')
  for row in [first,second]:self.assertEqual((21,18,3,'partial'),(row['expectedCount'],row['completed'],row['missingCount'],row['replenishmentState']))
 def test_zero_base_does_not_double_count_on_restore(self):
  shell=self.shell(base=0);first=self.save(shell,{'expectedCount':21,'completed':0},'finished');second=self.save(shell,first,'finished');self.assertEqual(2,second['completed'])
 def test_import_waiting_preserves_existing_parent_progress(self):
  row=self.save(self.shell(success=0),{'expectedCount':21,'completed':16},'waiting');self.assertEqual(16,row['completed']);self.assertEqual(21,row['expectedCount'])
 def test_partial_close_keeps_downstream_blocked_and_retires_child(self):
  shell=self.shell();shell._set_remote_pipeline_status=Mock()
  parent={'expectedCount':21,'completed':18,'missingCount':3}
  with patch.object(wb,'load_project_download_statuses',return_value={'items':{'fixture-parent':parent}}),patch.object(wb,'mark_replenishment_merged') as merged,patch.object(wb,'load_task_record') as load:
   self.assertEqual((3,False),wb.Workbench._close_step12_replenishment(shell));merged.assert_called_once_with('fixture-child','fixture-parent');load.assert_not_called()
