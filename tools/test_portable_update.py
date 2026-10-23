"""Real PowerShell update/rollback against synthetic portable folders only."""
import hashlib,json,os,shutil,subprocess,tempfile,unittest
from pathlib import Path
SCRIPT=Path(__file__).resolve().parents[1]/'portable_submitter/更新V14.ps1'
class UpdateTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='portable-v14-test-');self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.source=self.root/'new';self.target=self.root/'old with spaces';self.source.mkdir();self.target.mkdir()
  for name in ('正式任务递交器','ExampleBrand自然流任务递交器','程序/统一任务递交器'):(self.target/name).mkdir(parents=True)
  self.exe='程序/统一任务递交器/Unified-Task-Submitter.exe'
  (self.target/self.exe).write_bytes(b'old-program')
  (self.target/'启动统一任务递交器.cmd').write_bytes(b'old-launcher')
  self.data={}
  for folder,name in (('正式任务递交器','saved_task_box.json'),('ExampleBrand自然流任务递交器','examplebrand_saved_task_box.json'),('正式任务递交器','task_submitter_config.json'),('ExampleBrand自然流任务递交器','task_submitter_config.json'),('','custom-user-file.bin')):
   rel=Path(folder)/name;value=b'fixture-private-config-or-user-data';(self.target/rel).write_bytes(value);self.data[rel]=value
  shutil.copyfile(SCRIPT,self.source/SCRIPT.name)
  p=self.source/self.exe;p.parent.mkdir(parents=True);p.write_bytes(b'new-program')
  (self.source/'new-module.py').write_bytes(b'# new program\n')
  self.manifest={'files':[{'path':n,'sha256':hashlib.sha256((self.source/n).read_bytes()).hexdigest()} for n in (self.exe,'new-module.py')]}
  self.write_manifest()
 def write_manifest(self):(self.source/'program-files.json').write_text(json.dumps(self.manifest,ensure_ascii=False),'utf-8')
 def run_ps(self,*args):
  env=os.environ.copy();env['PSModulePath']=r'C:\Windows\System32\WindowsPowerShell\v1.0\Modules;C:\Program Files\WindowsPowerShell\Modules'
  result=subprocess.run([os.environ.get('SystemRoot',r'C:\Windows')+r'\System32\WindowsPowerShell\v1.0\powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(self.source/SCRIPT.name),*args],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,env=env)
  self.assertNotIn(b'fixture-private-config-or-user-data',result.stdout+result.stderr)
  return result
 def test_real_update_complete_backup_user_data_preservation_and_rollback(self):
  result=self.run_ps('-Target',str(self.target));self.assertEqual(0,result.returncode,result.stderr.decode(errors='replace'))
  self.assertEqual(b'new-program',(self.target/self.exe).read_bytes())
  backups=list(self.root.glob('old with spaces-backup-before-V14-*'));self.assertEqual(1,len(backups));backup=backups[0]
  for rel,value in self.data.items():self.assertEqual(value,(self.target/rel).read_bytes());self.assertEqual(value,(backup/rel).read_bytes())
  # User edits after update must survive a program rollback too.
  rel=Path('正式任务递交器/saved_task_box.json');(self.target/rel).write_bytes(b'newer-user-data')
  result=self.run_ps('-Rollback',str(backup/'V14-update-operation.json'));self.assertEqual(0,result.returncode,result.stderr.decode(errors='replace'))
  self.assertEqual(b'old-program',(self.target/self.exe).read_bytes());self.assertFalse((self.target/'new-module.py').exists());self.assertEqual(b'newer-user-data',(self.target/rel).read_bytes())
 def test_tampered_package_refused_before_backup_and_changes(self):
  (self.source/self.exe).write_bytes(b'tampered');result=self.run_ps('-Target',str(self.target));self.assertNotEqual(0,result.returncode);self.assertEqual(b'old-program',(self.target/self.exe).read_bytes());self.assertEqual([],list(self.root.glob('*backup-before-V14-*')))
 def test_manifest_cannot_replace_configuration(self):
  (self.source/'task_submitter_config.json').write_bytes(b'forbidden')
  self.manifest['files'].append({'path':'task_submitter_config.json','sha256':hashlib.sha256(b'forbidden').hexdigest()});self.write_manifest()
  result=self.run_ps('-Target',str(self.target));self.assertNotEqual(0,result.returncode);self.assertEqual([],list(self.root.glob('*backup-before-V14-*')))
if __name__=='__main__':unittest.main(verbosity=2)
