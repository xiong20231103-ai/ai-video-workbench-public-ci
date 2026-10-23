"""Rebuild all three windowed clients with an existing PyInstaller environment.

No installation, GUI launch, configuration/state copying or production writes.
Output must be new. Every compiler child is created without a console window.
"""
import argparse,hashlib,json,os,shutil,struct,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
def hidden_children():
 original=subprocess.Popen
 class Hidden(original):
  def __init__(self,*args,**kw):
   kw['creationflags']=kw.get('creationflags',0)|subprocess.CREATE_NO_WINDOW
   info=kw.get('startupinfo') or subprocess.STARTUPINFO()
   info.dwFlags|=subprocess.STARTF_USESHOWWINDOW;info.wShowWindow=0
   kw['startupinfo']=info
   super().__init__(*args,**kw)
 subprocess.Popen=Hidden
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--work',type=Path,required=True);args=parser.parse_args()
 if os.name!='nt':raise SystemExit('Windows builder required')
 for p in (args.output,args.work):
  if p.exists():raise SystemExit('Refusing existing build/output directory')
  p.mkdir(parents=True)
 os.environ['PYTHONDONTWRITEBYTECODE']='1'
 os.environ['PYINSTALLER_CONFIG_DIR']=str(args.work/'config')
 # Compiler helper interpreters inherit this policy, without changing installed packages.
 bootstrap=args.work/'hidden-bootstrap';bootstrap.mkdir()
 (bootstrap/'sitecustomize.py').write_text('import subprocess\n'+__import__('inspect').getsource(hidden_children)+'\nhidden_children()\n','utf-8')
 os.environ['PYTHONPATH']=str(bootstrap)+os.pathsep+os.environ.get('PYTHONPATH','')
 hidden_children()
 source=root/'portable_submitter'
 shutil.copytree(source,args.output,dirs_exist_ok=True)
 from PyInstaller.__main__ import run
 builds=[('AI-Video-Task-Submitter','正式任务递交器/company_task_submitter.pyw','正式任务递交器/程序',False),('ExampleBrand-Natural-Flow-Task-Submitter','ExampleBrand自然流任务递交器/ExampleBrand自然流任务投递器.pyw','ExampleBrand自然流任务递交器/程序',False),('Unified-Task-Submitter','unified_task_submitter.pyw','程序',True)]
 report=[]
 for name,script,dist,rename in builds:
  p=args.output/script
  argv=['--noconfirm','--clean','--windowed','--name',name,'--distpath',str(args.output/dist),'--workpath',str(args.work/name),'--specpath',str(args.work),'--paths',str(p.parent)]
  if name=='AI-Video-Task-Submitter':argv+=['--add-data',str(p.parent/'project_task_templates.json')+os.pathsep+'.']
  argv.append(str(p));run(argv)
  folder=args.output/dist/name
  if rename:
   target=args.output/'程序/统一任务递交器';folder.rename(target);folder=target
  for rel in ('_internal/_tkinter.pyd','_internal/tcl86t.dll','_internal/tk86t.dll','_internal/_tcl_data/init.tcl','_internal/_tk_data/tk.tcl'):
   if not (folder/rel).is_file():raise RuntimeError('Incomplete Tk runtime: '+rel)
  exe=folder/(name+'.exe');data=exe.read_bytes();pe=struct.unpack_from('<I',data,0x3c)[0]
  # PE optional-header subsystem 2 = Windows GUI, never a console executable.
  if data[pe:pe+4]!=b'PE\0\0' or struct.unpack_from('<H',data,pe+24+68)[0]!=2:raise RuntimeError('Executable is not windowed')
  if (p.parent/'task_submitter_config.example.json').is_file():shutil.copyfile(p.parent/'task_submitter_config.example.json',folder/'task_submitter_config.example.json')
  report.append({'name':name,'exe':exe.relative_to(args.output).as_posix(),'sha256':hashlib.sha256(data).hexdigest(),'subsystem':2,'tkRuntimeVerified':True})
 (args.work/'build-result.json').write_text(json.dumps({'python':sys.version,'pyinstaller':__import__('PyInstaller').__version__,'builds':report,'guiLaunched':False,'productionWrites':False},ensure_ascii=False,indent=2),'utf-8')
 print(json.dumps({'built':3,'windowed':True,'guiLaunched':False}))
if __name__=='__main__':main()
