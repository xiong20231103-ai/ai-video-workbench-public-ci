"""Tailnet HTTPS submission with durable receipts and GET-only recovery.

No token, proxy, insecure TLS or redirected requests. Pending input is local to
this submitter until a real receiver receipt arrives; no media is copied.
"""
from __future__ import annotations
import base64,hashlib,http.client,json,os,time,uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request,ProxyHandler,HTTPRedirectHandler,build_opener

MAX_BODY=2*1024*1024
MAX_WIRE=3*1024*1024 # Includes base64 result envelope
MAX_PENDING=MAX_BODY*3+65536 # Input plus validate/batches receipts
class PendingSubmission(Exception):pass
class SubmissionRejected(ValueError):pass
class HealthResponseError(ValueError):
 def __init__(self,status):self.status=status;super().__init__("健康响应不是有效 JSON")
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None

def origin(value):
 parts=urlsplit(value)
 if parts.scheme!='https' or not parts.hostname or not parts.hostname.endswith('.ts.net') or parts.username or parts.password or parts.port not in (None,443) or parts.path not in ('','/') or parts.query or parts.fragment:
  raise ValueError('云端地址必须是 Tailscale 的 HTTPS 根地址，例如 https://service.tailexample.ts.net')
 return value.rstrip('/')
def encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def atomic(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
 try:
  with temporary.open('xb') as handle:handle.write(encode(value));handle.flush();os.fsync(handle.fileno())
  for attempt in range(10):
   try:os.replace(temporary,path);return
   except PermissionError:
    if attempt==9:raise
    time.sleep(.2)
 finally:temporary.unlink(missing_ok=True)
def load_pending(path):
 path=Path(path)
 try:
  for attempt in range(10):
   try:
    raw=path.read_bytes()
    if len(raw)>MAX_PENDING:raise ValueError('待查询回执文件超过上限')
    value=json.loads(raw)
    if not isinstance(value,dict) or value.get('schema')!=1 or not isinstance(value.get('payload'),dict) or not isinstance(value.get('stages'),dict):raise ValueError('待查询回执格式无效；请保留文件并人工核对')
    origin(value.get('origin',''));return value
   except (PermissionError,json.JSONDecodeError):
    if attempt==9:raise
    time.sleep(.2)
 except FileNotFoundError:return None

class CloudSubmission:
 def __init__(self,url,pending_file,transport=None,clock=time.monotonic,sleep=time.sleep):
  self.origin=origin(url);self.path=Path(pending_file);self.transport=transport or build_opener(ProxyHandler({}),NoRedirect());self.clock=clock;self.sleep=sleep
 def request(self,path,payload=None,request_id=None):
  # All result links are built locally; server-supplied URLs cannot redirect a request.
  if not path.startswith(('/api/submissions/','/api/submission-requests/')) and path not in {'/api/health','/api/batches','/api/batches/validate'}:raise ValueError('未授权的递交路径')
  headers={'Accept':'application/json'}
  if payload is not None:headers.update({'Content-Type':'application/json','X-Client-Request-ID':request_id})
  request=Request(self.origin+path,data=encode(payload) if payload is not None else None,headers=headers,method='POST' if payload is not None else 'GET')
  try:response=self.transport.open(request,timeout=35 if payload is not None else 10)
  except HTTPError as error:response=error
  with response:
   length=getattr(response,'headers',{}).get('Content-Length')
   if length is not None and (len(length)>10 or not length.isdigit() or int(length)>MAX_WIRE):raise OSError('回执长度无效，保留待查询状态')
   raw=response.read(MAX_WIRE+1)
   if len(raw)>MAX_WIRE or length is not None and len(raw)!=int(length):raise OSError('回执被截断或过大，保留待查询状态')
   try:return response.code,json.loads(raw)
   except (ValueError,UnicodeError):
    if path=='/api/health':raise HealthResponseError(response.code) from None
    raise
 def prepare(self,payload):
  if len(encode(payload))>MAX_BODY:raise ValueError('任务箱超过2MiB上限')
  pending=load_pending(self.path)
  if pending:
   if pending['origin']!=self.origin or encode(pending['payload'])!=encode(payload):raise PendingSubmission('存在另一份待核对的递交记录；先查询原回执，不会覆盖或重发')
   return pending
  value={'schema':1,'origin':self.origin,'payload':payload,'stages':{},'phase':'prepared','createdAt':time.time()};atomic(self.path,value);return value
 def _record(self,pending,phase,notify):
  pending['phase']=phase;atomic(self.path,pending);notify(phase)
 def _decode(self,receipt):
  if receipt.get('state')!='completed':raise PendingSubmission('云端状态：'+str(receipt.get('state'))+'；请在网页核对，不会自动重发')
  result=receipt.get('result') or {}
  if result.get('kind')!='http':raise PendingSubmission('家里回执结果不明，不会自动重发')
  try:
   raw=base64.b64decode(result['bodyBase64'],validate=True)
   if len(raw)>MAX_BODY:raise ValueError()
   return result['httpStatus'],json.loads(raw)
  except (KeyError,ValueError,TypeError):raise PendingSubmission('家里回执格式无效，请人工核对') from None
 def stage(self,pending,path,notify,wait_seconds,query_only=False):
  stages=pending['stages'];entry=stages.get(path)
  if entry and 'httpStatus' in entry:return entry['httpStatus'],entry['response']
  if not entry:
   if query_only:raise PendingSubmission("此阶段尚未提交；仅查询不会发送任务。需要递交时请点击“继续未提交任务”并确认。")
   # Same batch + stage always has the same ID. Changed contents cannot silently
   # create a second request after reload or an ambiguous timeout.
   request_id=hashlib.sha256((path+'\n'+str((pending['payload'].get('clientSubmissionId') or pending['payload']['batchId']))).encode()).hexdigest()
   entry={'requestId':request_id,'attempted':True};stages[path]=entry
   self._record(pending,'submitting',notify) # fsync BEFORE the only POST attempt
   try:
    status,answer=self.request(path,pending['payload'],request_id)
    if status==202 and answer.get('cloudCommand'):
     entry['cloudCommandId']=answer['cloudCommandId'];self._record(pending,'cloud_received',notify)
    else:
     entry.update(httpStatus=status,response=answer);atomic(self.path,pending);return status,answer
   except (OSError,http.client.HTTPException,ValueError):self._record(pending,'receipt_lookup',notify)
  deadline=self.clock()+wait_seconds
  while self.clock()<deadline:
   try:
    status,answer=self.request('/api/submissions/'+entry['cloudCommandId'] if entry.get('cloudCommandId') else '/api/submission-requests/'+entry['requestId'])
    if status==200:
     if answer.get('cloudCommandId'):entry['cloudCommandId']=answer['cloudCommandId']
     state=answer.get('state');self._record(pending,'cloud_received' if state in {'queued','leased'} else 'waiting_home' if state=='sent' else str(state),notify)
     if state not in {'queued','leased','sent'}:
      code,body=self._decode(answer);entry.update(httpStatus=code,response=body);atomic(self.path,pending);return code,body
    elif status!=404:notify('receipt_lookup')
   except (OSError,http.client.HTTPException,json.JSONDecodeError):notify('receipt_lookup')
   self.sleep(1)
  raise PendingSubmission('仍在查询家里回执。输入和请求编号已保存；再次点击只查询，不会重复 POST。')
 def check_health(self,pending,expected_total,expected_projects=None):
  diagnostic={'checkedAt':time.time(),'httpStatus':None,'differences':[]}
  try:
   status,health=self.request('/api/health');diagnostic['httpStatus']=status
   if status!=200:diagnostic['differences'].append('HTTP '+str(status)+'（期望 200）')
   if not isinstance(health,dict):diagnostic['differences'].append('响应类型：'+type(health).__name__+'（期望 object）')
   elif expected_total is not None:
    # Only whitelisted scalar facts are saved; never persist arbitrary response/error text.
    for key,expected in (('ok',True),('contractVersion',2),('formalBatchTotal',expected_total)):
     actual=health.get(key)
     if type(actual) is not type(expected) or actual!=expected:
      safe=str(actual) if actual is None or type(actual) in (int,bool) else '['+type(actual).__name__+']'
      diagnostic['differences'].append(key+'='+safe+'（期望 '+str(expected)+'）')
    totals=health.get('projectTotals')
    if not isinstance(totals,dict):diagnostic['differences'].append('projectTotals 类型不是 object')
    else:
     for key,expected in (expected_projects or {'ProjectD':None}).items():
      actual=totals.get(key)
      if key not in totals or expected is not None and (type(actual) is not int or actual!=expected):
       safe=str(actual) if actual is None or type(actual) in (int,bool) else '['+type(actual).__name__+']'
       diagnostic['differences'].append('projectTotals.'+key+'='+safe+'（期望 '+str(expected if expected is not None else '存在')+'）')
  except HealthResponseError as error:
   diagnostic['httpStatus']=error.status;diagnostic['differences'].append('响应不是有效 JSON')
  except (OSError,http.client.HTTPException,ValueError) as error:
   diagnostic['errorCategory']=type(error).__name__
   diagnostic['differences'].append('健康请求失败（网络、超时或响应长度异常）；未取得可验证响应')
  pending['lastHealthCheck']=diagnostic;atomic(self.path,pending)
  if diagnostic['differences']:
   raise PendingSubmission('健康检查未通过（HTTP '+str(diagnostic['httpStatus'] or '未取得')+'）：'+'；'.join(diagnostic['differences'])+'。任务箱和原编号已保存，尚未提交。')
 def run(self,payload,notify=lambda _:None,wait_seconds=180,expected_total=None,query_only=False,expected_projects=None):
  if query_only:
   pending=load_pending(self.path)
   if not pending:raise PendingSubmission('没有已保存的递交记录；仅查询不会新建任务或提交。')
   if pending['origin']!=self.origin or encode(pending['payload'])!=encode(payload):
    raise PendingSubmission('待查询记录身份不符；保留原记录人工核对。')
  else:pending=self.prepare(payload)
  # Query mode never probes health and never starts an unattempted stage.
  if not pending['stages'] and not query_only:self.check_health(pending,expected_total,expected_projects)
  status,validation=self.stage(pending,'/api/batches/validate',notify,wait_seconds,query_only=query_only)
  if not 200<=status<300 or not validation.get('ok'):
   self._record(pending,'rejected',notify);raise SubmissionRejected(str(validation.get('detail') or validation.get('message') or '家里校验未通过'))
  status,result=self.stage(pending,'/api/batches',notify,wait_seconds,query_only=query_only)
  if not 200<=status<300 or not result.get('ok'):
   self._record(pending,'rejected',notify);raise SubmissionRejected(str(result.get('detail') or result.get('message') or '家里未确认接收'))
  self._record(pending,'home_received',notify)
  return result
 def complete(self):
  pending=load_pending(self.path)
  if not pending or pending.get('phase')!='home_received':raise ValueError('只有真实家里回执后才能结束待查询记录')
  self.path.unlink()

 def release(self):
  pending=load_pending(self.path)
  if not pending:return
  for path,entry in pending['stages'].items():
   if 'httpStatus' in entry:continue
   status,answer=self.request('/api/submissions/'+entry['cloudCommandId'] if entry.get('cloudCommandId') else '/api/submission-requests/'+entry['requestId'])
   if status!=200 or answer.get('state') not in {'completed','cancelled','expired','not_sent','resolved_unknown'}:
    raise PendingSubmission('原请求尚未终结。请在网页撤销或人工对账；请求ID：'+entry['requestId'])
  self.path.unlink()
