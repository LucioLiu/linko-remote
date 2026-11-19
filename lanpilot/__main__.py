import argparse,json
from pathlib import Path
from .core import Bridge
p=argparse.ArgumentParser(prog='邻控');s=p.add_subparsers(dest='command',required=True)
s.add_parser('devices')
s.add_parser('ssh-list')
x=s.add_parser('ssh-add');x.add_argument('name');x.add_argument('host');x.add_argument('--user',default='');x.add_argument('--port',type=int,default=22)
x=s.add_parser('ssh-open');x.add_argument('id')
x=s.add_parser('ssh-rename');x.add_argument('id');x.add_argument('name')
x=s.add_parser('rename');x.add_argument('device');x.add_argument('name')
x=s.add_parser('pair-code');x.add_argument('name');x.add_argument('--minutes',type=int,default=10)
x=s.add_parser('windows-client');x.add_argument('installer',type=Path);x.add_argument('--controller',default='这台电脑');x.add_argument('--isolated-test',action='store_true');x.add_argument('--generic',action='store_true')
for a in ['service-install','service-start','service-stop']:s.add_parser(a)
x=s.add_parser('enroll');x.add_argument('name');x.add_argument('public_xml',type=Path);x.add_argument('installer',type=Path)
x=s.add_parser('invite');x.add_argument('name');x.add_argument('installer',type=Path);x.add_argument('--minutes',type=int,default=15);x.add_argument('--isolated-test',action='store_true',help='isolated read-only acceptance profile; no user startup or shortcuts')
x=s.add_parser('invite-route');x.add_argument('--server');x.add_argument('--port',type=int);x.add_argument('--lan-only',action='store_true')
x=s.add_parser('adopt');x.add_argument('name');x.add_argument('runtime')
x=s.add_parser('job');x.add_argument('device');x.add_argument('manifest',type=Path)
x=s.add_parser('stage');x.add_argument('device');x.add_argument('scope');x.add_argument('files',nargs='*',type=Path);x.add_argument('--input',nargs=2,action='append',default=[],metavar=('REMOTE_NAME','MAC_PATH'))
for a in ['status','observe','recover','action']:
 x=s.add_parser(a);x.add_argument('device')
 if a=='observe':x.add_argument('--image-format',choices=['png','compact'])
 if a=='recover':x.add_argument('request_id')
 if a=='action':x.add_argument('json')
a=p.parse_args();b=Bridge()
if a.command.startswith('service-'):
 from . import service
 r=getattr(service,a.command.split('-')[1])()
elif a.command=='devices':r=b.health_all()
elif a.command.startswith('ssh-'):
 from .ssh_profiles import profiles,save,open_terminal,rename
 try:
  if a.command=='ssh-list':r=profiles(b)
  elif a.command=='ssh-add':r=save(b,a.name,a.host,a.user,a.port)
  elif a.command=='ssh-rename':r=rename(b,a.id,a.name)
  else:r=open_terminal(b,a.id)
 except Exception as e:p.exit(1,str(e)+'\n')
elif a.command=='rename':
 try:r=b.rename(a.device,a.name)
 except Exception as e:p.exit(1,str(e)+'\n')
elif a.command in ('pair-code','windows-client'):
 from .invite_gateway import ensure_endpoint
 from .pairing_codes import issue_code,export_client
 try:
  if a.command=='pair-code' or not a.generic:ensure_endpoint(b)
  r=issue_code(b,a.name,a.minutes) if a.command=='pair-code' else export_client(b,a.installer,a.controller,a.isolated_test,a.generic)
 except Exception as e:p.exit(1,str(e)+'\n')
elif a.command in ('invite','invite-route'):
 from .invite_gateway import ensure_endpoint
 from .invitations import issue
 try:
  if a.command=='invite-route':r=ensure_endpoint(b,a.server,a.port,a.lan_only)
  else:
   ensure_endpoint(b)
   r=issue(b,a.name,a.installer,a.minutes,a.isolated_test)
 except Exception as e:p.exit(1,str(e)+'\n')
elif a.command=='enroll':
 from .enrollment import enroll
 try:r=enroll(b,a.name,a.public_xml,a.installer)
 except Exception as e:p.exit(1,str(e)+'\n')
elif a.command=='adopt':r=b.adopt(a.name,a.runtime)
elif a.command=='job':
 with a.manifest.open('rb') as stream:raw=stream.read(3000001)
 if len(raw)>3000000:p.exit(1,'Job manifest exceeds size limit\n')
 r=b.request(a.device,{'action':'job','manifest':json.loads(raw)})
elif a.command=='stage':
 from .jobs import stage_manifest
 r=b.request(a.device,{'action':'job','manifest':stage_manifest(a.scope,a.files+[{'name':n,'path':p} for n,p in a.input])})
elif a.command=='recover':r=b.request(a.device,None,a.request_id)
elif a.command=='action':r=b.request(a.device,json.loads(a.json))
else:
 request={'action':a.command}
 if getattr(a,'image_format',None):request['image_format']=a.image_format
 r=b.request(a.device,request)
print(json.dumps(r,ensure_ascii=False,indent=2))
