"""Build without launching/activating any graphical app."""
import plistlib,subprocess,sys,os,shlex,shutil
from pathlib import Path
root=Path(__file__).resolve().parents[1]
out=Path(sys.argv[1]).resolve();contents=out/'Contents'
for sub in ['MacOS','Resources']:(contents/sub).mkdir(parents=True,exist_ok=True)
(contents/'Info.plist').write_bytes(plistlib.dumps({'CFBundleName':'邻控','CFBundleDisplayName':'邻控','CFBundleIdentifier':'local.lanpilot.desktop','CFBundleVersion':'0.6.0','CFBundleShortVersionString':'0.6.0','CFBundleExecutable':'LanPilot','NSHighResolutionCapable':True}))
code=contents/'Resources/code'
code.mkdir(exist_ok=True)
for n in ['relay.py','desktop.py','lanpilot_mcp.py','lanpilot_service.py']:shutil.copyfile(root/n,code/n)
shutil.copytree(root/'lanpilot',code/'lanpilot',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
shutil.copytree(root/'windows',code/'windows',dirs_exist_ok=True)
script=contents/'Resources/lanpilot';script.write_text('#!/bin/sh\ncd '+'"$(dirname "$0")/code"'+'\nexec /usr/bin/python3 -m lanpilot "$@"\n');script.chmod(0o755)
subprocess.run(['swiftc',str(root/'lanpilot/mac/App.swift'),'-o',str(contents/'MacOS/LanPilot'),'-framework','Cocoa'],check=True)
subprocess.run(['codesign','--force','--deep','--sign','-',str(out)],check=True)
print(out)
