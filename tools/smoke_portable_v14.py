"""Run explicit headless tests on a private, never-shown Windows desktop."""
import argparse,ctypes,json,os,subprocess,uuid
from ctypes import wintypes as W
from pathlib import Path
class Startup(ctypes.Structure):
 _fields_=[('cb',W.DWORD),('lpReserved',W.LPWSTR),('lpDesktop',W.LPWSTR),('lpTitle',W.LPWSTR),('dwX',W.DWORD),('dwY',W.DWORD),('dwXSize',W.DWORD),('dwYSize',W.DWORD),('dwXCountChars',W.DWORD),('dwYCountChars',W.DWORD),('dwFillAttribute',W.DWORD),('dwFlags',W.DWORD),('wShowWindow',W.WORD),('cbReserved2',W.WORD),('lpReserved2',ctypes.POINTER(ctypes.c_byte)),('hStdInput',W.HANDLE),('hStdOutput',W.HANDLE),('hStdError',W.HANDLE)]
class Process(ctypes.Structure):
 _fields_=[('hProcess',W.HANDLE),('hThread',W.HANDLE),('dwProcessId',W.DWORD),('dwThreadId',W.DWORD)]
def main():
 p=argparse.ArgumentParser();p.add_argument('--package',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);a=p.parse_args()
 package=a.package.resolve();evidence=a.evidence.resolve();evidence.mkdir(parents=True,exist_ok=False)
 user=ctypes.WinDLL('user32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
 user.CreateDesktopW.argtypes=[W.LPCWSTR,W.LPCWSTR,ctypes.c_void_p,W.DWORD,W.DWORD,ctypes.c_void_p];user.CreateDesktopW.restype=W.HANDLE
 user.CloseDesktop.argtypes=[W.HANDLE];user.CloseDesktop.restype=W.BOOL
 kernel.CreateProcessW.argtypes=[W.LPCWSTR,W.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,W.BOOL,W.DWORD,ctypes.c_void_p,W.LPCWSTR,ctypes.POINTER(Startup),ctypes.POINTER(Process)];kernel.CreateProcessW.restype=W.BOOL
 kernel.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD];kernel.WaitForSingleObject.restype=W.DWORD
 kernel.GetExitCodeProcess.argtypes=[W.HANDLE,ctypes.POINTER(W.DWORD)];kernel.GetExitCodeProcess.restype=W.BOOL
 kernel.TerminateProcess.argtypes=[W.HANDLE,W.UINT];kernel.CloseHandle.argtypes=[W.HANDLE]
 name='OpsV14Smoke-'+uuid.uuid4().hex;desktop=user.CreateDesktopW(name,None,None,0,0x000F01FF,None)
 if not desktop:raise ctypes.WinError(ctypes.get_last_error())
 reports=[]
 try:
  builds=[('formal','正式任务递交器/程序/AI-Video-Task-Submitter/AI-Video-Task-Submitter.exe'),('examplebrand','ExampleBrand自然流任务递交器/程序/ExampleBrand-Natural-Flow-Task-Submitter/ExampleBrand-Natural-Flow-Task-Submitter.exe'),('unified','程序/统一任务递交器/Unified-Task-Submitter.exe')]
  for kind,rel in builds:
   exe=package/rel;output=evidence/(kind+'.json');scratch=evidence/(kind+'-scratch');scratch.mkdir()
   env=dict(os.environ,LOCALAPPDATA=str(scratch),APPDATA=str(scratch),TEMP=str(scratch),TMP=str(scratch),PYTHONDONTWRITEBYTECODE='1')
   envblock=ctypes.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted(env.items(),key=lambda kv:kv[0].upper()))+'\0\0')
   si=Startup();si.cb=ctypes.sizeof(si);si.lpDesktop=name;si.dwFlags=1;si.wShowWindow=0;pi=Process()
   cmd=ctypes.create_unicode_buffer(subprocess.list2cmdline([str(exe),'--v14-self-test',str(output)]))
   if not kernel.CreateProcessW(str(exe),cmd,None,None,False,0x08000400,envblock,str(scratch),ctypes.byref(si),ctypes.byref(pi)):raise ctypes.WinError(ctypes.get_last_error())
   try:
    if kernel.WaitForSingleObject(pi.hProcess,30000)!=0:
     kernel.TerminateProcess(pi.hProcess,124);kernel.WaitForSingleObject(pi.hProcess,5000);raise RuntimeError('Private desktop smoke timed out: '+kind)
    code=W.DWORD();kernel.GetExitCodeProcess(pi.hProcess,ctypes.byref(code))
    if code.value or not output.exists():raise RuntimeError('Headless executable failed: '+kind+', code '+str(code.value))
   finally:kernel.CloseHandle(pi.hThread);kernel.CloseHandle(pi.hProcess)
   result=json.loads(output.read_text('utf-8'))
   assert result['version']==14 and result['frozen'] and not result['tkWindowCreated'] and not result['networkUsed'] and result['consoleWindow']==0,result
   if kind=='unified':assert result['packageRootResolved']
   reports.append(result)
 finally:user.CloseDesktop(desktop)
 (evidence/'result.json').write_text(json.dumps({'privateDesktopNeverShown':True,'results':reports},indent=2),'utf-8')
 print(json.dumps({'executablesPassed':len(reports),'consoleWindows':0,'tkWindows':0,'privateDesktopNeverShown':True}))
if __name__=='__main__':main()
