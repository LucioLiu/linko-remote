"""User-owned background service. Does not launch any graphical surface."""
import os,sys,plistlib,subprocess,time
from pathlib import Path
from .core import DEFAULT_HOME
LABEL='local.lanpilot.bridge'
PLIST=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
def install():
 root=Path(__file__).resolve().parents[1];DEFAULT_HOME.mkdir(parents=True,exist_ok=True,mode=0o700);PLIST.parent.mkdir(parents=True,exist_ok=True)
 data={'Label':LABEL,'ProgramArguments':['/usr/bin/python3',str(root/'lanpilot_service.py')],'WorkingDirectory':str(root),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'StandardOutPath':str(DEFAULT_HOME/'service.log'),'StandardErrorPath':str(DEFAULT_HOME/'service.err')}
 PLIST.write_bytes(plistlib.dumps(data));PLIST.chmod(0o600)
 return {'installed':True}
def start():
 domain=f'gui/{os.getuid()}'
 if subprocess.run(['launchctl','print',domain+'/'+LABEL],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:return {'running':True}
 if not PLIST.exists():install()
 r=subprocess.run(['launchctl','bootstrap',domain,str(PLIST)],capture_output=True)
 return {'running':r.returncode==0}
def stop():
 r=subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{LABEL}'],capture_output=True)
 if r.returncode==0:
  for _ in range(100):
   if subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{LABEL}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0:break
   time.sleep(0.1)
 return {'stopped':r.returncode==0}
