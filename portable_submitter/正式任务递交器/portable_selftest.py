"""Explicit headless executable smoke test; never creates Tk or sends requests."""
import ctypes,json,sys
from pathlib import Path
import tkinter
def run(kind,package_root=None):
    if len(sys.argv)!=3 or sys.argv[1]!='--v14-self-test':return False
    report=Path(sys.argv[2]).resolve()
    def audit(event,args):
        if event in {'socket.connect','socket.bind','subprocess.Popen','os.system'}:raise RuntimeError('Self-test forbids network/processes')
    sys.addaudithook(audit)
    tcl=tkinter.Tcl() # Tcl interpreter only: no Tk root, visible or withdrawn.
    result={'version':14,'kind':kind,'frozen':bool(getattr(sys,'frozen',False)),
            'tclVersion':tcl.eval('info patchlevel'),'consoleWindow':int(ctypes.windll.kernel32.GetConsoleWindow()),
            'tkWindowCreated':False,'networkUsed':False}
    if package_root is not None:
        result['packageRootResolved']=all((package_root/n).is_dir() for n in ('正式任务递交器','ExampleBrand自然流任务递交器'))
    else:
        from submission_transport import origin
        result['httpsOriginValidated']=origin('https://service.tailexample.ts.net')=='https://service.tailexample.ts.net'
    report.write_text(json.dumps(result,ensure_ascii=True,indent=2),'utf-8')
    return True
