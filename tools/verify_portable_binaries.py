"""Compare frozen executable code to the shipped source without executing it."""
import argparse,hashlib,json,marshal,types
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
def fingerprint(code):
 def value(v):
  if isinstance(v,types.CodeType):return fingerprint(v)
  if isinstance(v,tuple):return tuple(value(x) for x in v)
  return v
 return (code.co_code,code.co_names,code.co_varnames,code.co_argcount,code.co_kwonlyargcount,code.co_posonlyargcount,code.co_flags,code.co_freevars,code.co_cellvars,tuple(value(c) for c in code.co_consts))
def main():
 p=argparse.ArgumentParser();p.add_argument('--package',type=Path,required=True);p.add_argument('--report',type=Path,required=True);a=p.parse_args();r=a.package.resolve()
 cases=[('正式任务递交器/company_task_submitter.pyw','正式任务递交器/程序/AI-Video-Task-Submitter/AI-Video-Task-Submitter.exe'),('ExampleBrand自然流任务递交器/ExampleBrand自然流任务投递器.pyw','ExampleBrand自然流任务递交器/程序/ExampleBrand-Natural-Flow-Task-Submitter/ExampleBrand-Natural-Flow-Task-Submitter.exe'),('unified_task_submitter.pyw','程序/统一任务递交器/Unified-Task-Submitter.exe')]
 reports=[]
 for source,exe in cases:
  src=r/source;binary=r/exe;archive=CArchiveReader(str(binary))
  embedded=marshal.loads(archive.extract(src.stem));compiled=compile(src.read_bytes(),str(src),'exec',optimize=0)
  assert fingerprint(embedded)==fingerprint(compiled),'Frozen/source mismatch: '+source
  pyz=archive.open_embedded_archive('PYZ.pyz')
  assert 'sitecustomize' not in pyz.toc,'Compiler bootstrap accidentally bundled'
  for name in ('submitter_instance_lock','local_import_bridge','submitter_connection','portable_selftest'):
   if src.name=='unified_task_submitter.pyw' and name=='submitter_connection':continue
   assert fingerprint(pyz.extract(name))==fingerprint(compile((src.parent/(name+'.py')).read_bytes(),name,'exec',optimize=0)),name
  if src.name!='unified_task_submitter.pyw':
   assert fingerprint(pyz.extract('submission_transport'))==fingerprint(compile((src.parent/'submission_transport.py').read_bytes(),'submission_transport','exec',optimize=0))
  reports.append({'exe':exe,'source':source,'sourceMatchesFrozenCode':True,'sharedModulesMatch':True,'exeSHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'sourceSHA256':hashlib.sha256(src.read_bytes()).hexdigest()})
 a.report.write_text(json.dumps(reports,ensure_ascii=False,indent=2),'utf-8');print(json.dumps({'verifiedExecutables':len(reports),'frozenCodeMatchesSources':True,'compilerBootstrapBundled':False}))
if __name__=='__main__':main()
