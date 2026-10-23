"""Run submitter/contract fixtures with production network/process/writes blocked."""
from pathlib import Path
import os,sys,tempfile,unittest
root=Path(__file__).resolve().parents[1];sys.dont_write_bytecode=True
assert Path.cwd().resolve()==root
sys.path.insert(0,str(root/'workbench'))
with tempfile.TemporaryDirectory(prefix='submitter-isolated-') as directory:
 scratch=Path(directory).resolve();os.environ['LOCALAPPDATA']=str(scratch/'localappdata')
 def safe(path):
  if isinstance(path,int):return True
  try:return any(Path(path).resolve().is_relative_to(parent) for parent in (root,scratch,Path(tempfile.gettempdir()).resolve()))
  except (ValueError,TypeError):return False
 def audit(event,args):
  paths=()
  if event=='open':
   path,mode,flags=args
   if mode and any(c in mode for c in 'wax+') or flags and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):paths=(path,)
  elif event in {'os.remove','os.rmdir','os.mkdir','os.chmod','os.utime','sqlite3.connect'}:paths=args[:1]
  elif event in {'os.rename','os.link','os.symlink'}:paths=args[:2]
  elif event in {'socket.connect','socket.bind','subprocess.Popen','os.system'}:raise RuntimeError('Production network/process blocked: '+event)
  if any(not safe(path) for path in paths):raise RuntimeError('Write outside worktree/temp blocked: '+event)
 sys.addaudithook(audit)
 names=('test_submission_transport','test_submitter_cloud_ui','test_task_contract','test_task_contract_v10')
 result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
 print('Isolation: no production writes, no live HTTP, no processes or Tk windows.')
 raise SystemExit(0 if result.wasSuccessful() else 1)
