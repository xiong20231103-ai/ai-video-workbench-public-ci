from pathlib import Path
import base64,json,unittest
from tempfile import TemporaryDirectory
from urllib.error import URLError
from submission_transport import CloudSubmission,PendingSubmission,SubmissionRejected,origin,load_pending

class Reply:
 def __init__(self,status,body):self.code=status;self.raw=json.dumps(body).encode()
 def read(self,size):return self.raw[:size]
 def __enter__(self):return self
 def __exit__(self,*args):pass
class FakeTransport:
 def __init__(self,answers):self.answers=list(answers);self.requests=[]
 def open(self,request,timeout):
  self.requests.append(request)
  value=self.answers.pop(0)
  if isinstance(value,Exception):raise value
  return value if isinstance(value,Reply) else Reply(*value)
def receipt(state='completed',status=200,body=None):
 return {'cloudCommand':True,'cloudCommandId':'fixture-command','state':state,'result':{'kind':'http','httpStatus':status,'bodyBase64':base64.b64encode(json.dumps(body or {'ok':True}).encode()).decode(),'contentType':'application/json'}}
class SubmissionTransportTests(unittest.TestCase):
 def setUp(self):
  self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'pending.json'
  self.payload={'batchId':'fixture-batch','clientSubmissionId':'fixture-batch','tasks':[{'taskId':'fixture-task'}]};self.now=0
 def clock(self):return self.now
 def sleep(self,seconds):self.now+=seconds
 def controller(self,transport):return CloudSubmission('https://service.tailexample.ts.net',self.path,transport,self.clock,self.sleep)
 def test_rejects_insecure_origin_userinfo_ports_redirect_targets(self):
  for url in ['http://service.tailexample.ts.net','https://evil.invalid','https://user:secret@service.tailexample.ts.net','https://service.tailexample.ts.net:8766','https://service.tailexample.ts.net/path']:
   with self.subTest(url=url),self.assertRaises(ValueError):origin(url)
  with self.assertRaises(ValueError):self.controller(FakeTransport([])).request('https://evil.invalid/api/batches')
 def test_immediate_legacy_receipt_health_and_no_token_headers(self):
  fake=FakeTransport([(200,{'ok':True,'contractVersion':2,'formalBatchTotal':123,'projectTotals':{'ProjectD':1}}),(200,{'ok':True}),(200,{'ok':True,'taskIds':['fixture-task']})]);controller=self.controller(fake)
  result=controller.run(self.payload,expected_total=123);self.assertTrue(result['ok']);self.assertEqual('home_received',load_pending(self.path)['phase'])
  self.assertEqual(['GET','POST','POST'],[r.method for r in fake.requests]);self.assertTrue(all(not r.has_header('X-task-token') and not r.has_header('Authorization') for r in fake.requests));controller.complete();self.assertFalse(self.path.exists())
 def test_202_is_cloud_received_not_home_success_then_get_final(self):
  fake=FakeTransport([(200,{}),(202,receipt('queued')),(200,receipt('sent')),(200,receipt()),(202,receipt('queued')),(200,receipt(body={'ok':True,'taskIds':['fixture-task']}))]);phases=[]
  self.assertTrue(self.controller(fake).run(self.payload,notify=phases.append)['ok']);self.assertIn('cloud_received',phases);self.assertIn('waiting_home',phases);self.assertEqual('home_received',phases[-1]);self.assertEqual(2,sum(r.method=='POST' for r in fake.requests))
 def test_lost_post_ack_reloads_queries_same_request_id_never_reposts(self):
  first=FakeTransport([(200,{}),URLError('fixture disconnect'),(404,{})]);controller=self.controller(first)
  with self.assertRaises(PendingSubmission):controller.run(self.payload,wait_seconds=1)
  entry=load_pending(self.path)['stages']['/api/batches/validate'];self.assertTrue(entry['attempted'])
  second=FakeTransport([(200,receipt()),(200,{'ok':True})]);self.assertTrue(self.controller(second).run(self.payload)['ok'])
  self.assertEqual('GET',second.requests[0].method);self.assertTrue(second.requests[0].full_url.endswith(entry['requestId']));self.assertEqual(1,sum(r.method=='POST' for r in first.requests));self.assertEqual(1,sum(r.method=='POST' for r in second.requests)) # only the DISTINCT /batches stage
 def test_unknown_and_changed_input_stay_locked_no_resubmit(self):
  fake=FakeTransport([(200,{}),(202,receipt('queued')),(200,receipt('unknown'))]);controller=self.controller(fake)
  with self.assertRaises(PendingSubmission):controller.run(self.payload)
  with self.assertRaises(PendingSubmission):controller.prepare({**self.payload,'tasks':[{'taskId':'different'}]})
  with self.assertRaises(ValueError):controller.complete()
  self.assertEqual(1,sum(r.method=='POST' for r in fake.requests))
 def test_validation_error_preserved_and_never_sends_batch(self):
  fake=FakeTransport([(200,{}),(422,{'detail':'fixture contract mismatch'})])
  with self.assertRaisesRegex(SubmissionRejected,'fixture contract mismatch'):self.controller(fake).run(self.payload)
  self.assertEqual('rejected',load_pending(self.path)['phase']);self.assertEqual(1,sum(r.method=='POST' for r in fake.requests))
 def test_contract_failure_does_not_post_and_bounds_input(self):
  fake=FakeTransport([(200,{'formalBatchTotal':1})])
  with self.assertRaises(PendingSubmission):self.controller(fake).run(self.payload,expected_total=123)
  self.assertEqual(['GET'],[r.method for r in fake.requests])
  with self.assertRaises(ValueError):self.controller(fake).prepare({**self.payload,'data':'x'*(2*1024*1024)})

 def test_truncated_valid_json_is_queried_not_accepted_and_never_reposted(self):
  reply=Reply(200,{'ok':True});reply.headers={'Content-Length':str(len(reply.raw)+10)}
  fake=FakeTransport([(200,{}),reply,(404,{})]);controller=self.controller(fake)
  with self.assertRaises(PendingSubmission):controller.run(self.payload,wait_seconds=1)
  self.assertNotEqual('home_received',load_pending(self.path)['phase']);self.assertEqual(1,sum(r.method=='POST' for r in fake.requests))
 def test_maximum_base64_reply_and_two_receipts_fit_bounded_resume_file(self):
  large={'ok':True,'fixture':'x'*(2*1024*1024-100)}
  fake=FakeTransport([(200,{}),(202,receipt('queued')),(200,receipt(body=large)),(202,receipt('queued')),(200,receipt(body=large))]);controller=self.controller(fake)
  self.assertTrue(controller.run(self.payload)['ok']);self.assertEqual('home_received',load_pending(self.path)['phase']);controller.complete();self.assertFalse(self.path.exists())

 def test_query_prepared_never_probes_health_posts_or_changes_identity(self):
  fake=FakeTransport([]);controller=self.controller(fake);before=controller.prepare(self.payload)
  with self.assertRaisesRegex(PendingSubmission,'尚未提交'):controller.run(self.payload,query_only=True)
  self.assertEqual([],fake.requests);self.assertEqual(before,load_pending(self.path))
 def test_query_without_record_creates_nothing(self):
  fake=FakeTransport([])
  with self.assertRaises(PendingSubmission):self.controller(fake).run(self.payload,query_only=True)
  self.assertFalse(self.path.exists());self.assertEqual([],fake.requests)
 def test_query_validation_receipt_cannot_start_unattempted_batch(self):
  first=FakeTransport([(200,{}),URLError('lost'),(404,{})]);controller=self.controller(first)
  with self.assertRaises(PendingSubmission):controller.run(self.payload,wait_seconds=1)
  original=load_pending(self.path)['stages']['/api/batches/validate']['requestId']
  query=FakeTransport([(200,receipt())])
  with self.assertRaisesRegex(PendingSubmission,'尚未提交'):self.controller(query).run(self.payload,query_only=True)
  self.assertEqual(['GET'],[r.method for r in query.requests]);self.assertTrue(query.requests[0].full_url.endswith(original))
  self.assertNotIn('/api/batches',load_pending(self.path)['stages'])
  resume=FakeTransport([(200,{'ok':True})]);self.assertTrue(self.controller(resume).run(self.payload)['ok'])
  self.assertEqual(['/api/batches'],[r.selector for r in resume.requests])
 def test_query_lost_batch_ack_only_gets_original_id_and_can_finish(self):
  first=FakeTransport([(200,{}),(200,{'ok':True}),URLError('lost'),(404,{})])
  with self.assertRaises(PendingSubmission):self.controller(first).run(self.payload,wait_seconds=1)
  original=load_pending(self.path)['stages']['/api/batches']['requestId']
  query=FakeTransport([(200,receipt())]);self.assertTrue(self.controller(query).run(self.payload,query_only=True)['ok'])
  self.assertEqual(['GET'],[r.method for r in query.requests]);self.assertTrue(query.requests[0].full_url.endswith(original))
 def test_query_unknown_stays_locked_and_never_posts(self):
  controller=self.controller(FakeTransport([]));pending=controller.prepare(self.payload)
  pending['stages']['/api/batches/validate']={'requestId':'original','attempted':True}
  from submission_transport import atomic
  atomic(self.path,pending);fake=FakeTransport([(200,receipt('unknown'))])
  with self.assertRaisesRegex(PendingSubmission,'unknown'):self.controller(fake).run(self.payload,query_only=True)
  self.assertEqual(['GET'],[r.method for r in fake.requests]);self.assertEqual('original',load_pending(self.path)['stages']['/api/batches/validate']['requestId'])
 def test_health_diagnostics_specific_safe_and_resume_preserves_ids(self):
  fake=FakeTransport([(503,{'detail':'SECRET_DO_NOT_STORE'})])
  with self.assertRaisesRegex(PendingSubmission,'HTTP 503'):self.controller(fake).run(self.payload,expected_total=79)
  self.assertNotIn('SECRET_DO_NOT_STORE',self.path.read_text(encoding="utf-8"));self.assertEqual(503,load_pending(self.path)['lastHealthCheck']['httpStatus'])
  fake=FakeTransport([(200,{'ok':True,'contractVersion':1,'formalBatchTotal':78,'projectTotals':{}})])
  with self.assertRaisesRegex(PendingSubmission,'contractVersion=1'):self.controller(fake).run(self.payload,expected_total=79)
  diag=load_pending(self.path)['lastHealthCheck'];self.assertEqual(3,len(diag['differences']));self.assertEqual({},load_pending(self.path)['stages'])
  good=FakeTransport([(200,{'ok':True,'contractVersion':2,'formalBatchTotal':79,'projectTotals':{'ProjectD':26}}),(200,{'ok':True}),(200,{'ok':True})])
  self.assertTrue(self.controller(good).run(self.payload,expected_total=79)['ok'])
  self.assertEqual(self.payload,load_pending(self.path)['payload']);self.assertEqual(['GET','POST','POST'],[r.method for r in good.requests])
 def test_health_invalid_json_type_network_and_hostile_field_are_safe(self):
  invalid=Reply(502,{});invalid.raw=b'<secret>SECRET_DO_NOT_STORE</secret>'
  for answer,expected in ((invalid,'JSON'),((200,[]),'list'),(URLError('SECRET_DO_NOT_STORE'),'请求失败'),((200,{'formalBatchTotal':'SECRET_DO_NOT_STORE'}),'formalBatchTotal=[str]')):
   with self.subTest(expected=expected):
    fake=FakeTransport([answer])
    with self.assertRaises(PendingSubmission) as error:self.controller(fake).run(self.payload,expected_total=79)
    self.assertIn(expected,str(error.exception));self.assertNotIn('SECRET_DO_NOT_STORE',str(error.exception));self.assertNotIn('SECRET_DO_NOT_STORE',self.path.read_text(encoding="utf-8"));self.assertEqual(['GET'],[r.method for r in fake.requests])

 def test_formal_project_contract_differences_are_individual_and_safe(self):
  fake=FakeTransport([(200,{'ok':True,'contractVersion':2,'formalBatchTotal':79,'projectTotals':{'ProjectD':25,'ProjectA':'SECRET_DO_NOT_STORE'}})])
  with self.assertRaises(PendingSubmission) as error:self.controller(fake).run(self.payload,expected_total=79,expected_projects={'ProjectD':26,'ProjectA':24,'ProjectB':13})
  self.assertIn('projectTotals.ProjectD=25',str(error.exception));self.assertIn('projectTotals.ProjectA=[str]',str(error.exception));self.assertIn('projectTotals.ProjectB=None',str(error.exception))
  self.assertNotIn('SECRET_DO_NOT_STORE',self.path.read_text(encoding='utf-8'));self.assertEqual(['GET'],[r.method for r in fake.requests])
