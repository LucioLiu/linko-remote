import base64
import hashlib
import http.client
import ssl
import threading
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from test_invitations import Invitations
from lanpilot.pairing_codes import issue_code,claim_code,export_client,RateLimited
from lanpilot.ssh_profiles import save,open_terminal,profiles

class PairingCodes(Invitations):
 def test_public_gateway_code_exchange_and_recovery(self):
  from lanpilot.invite_gateway import Gateway
  gateway=Gateway(self.b,'127.0.0.1',0)
  thread=threading.Thread(target=gateway.serve_forever,daemon=True);thread.start()
  code=issue_code(self.b,'public gateway')['code'].replace(' ','')
  def request(value):
   cx=http.client.HTTPSConnection(*gateway.server_address,context=ssl._create_unverified_context(),timeout=4)
   try:
    cx.request('POST','/claim-code',json.dumps({'public_key':self.xml}),headers={'Authorization':'Bearer '+value})
    reply=cx.getresponse();return reply.status,reply.read()
   finally:cx.close()
  try:
   self.assertEqual(request('000')[0],403)
   status,data=request(code);self.assertEqual(status,200)
   self.assertEqual(request(code),(status,data))
   for _ in range(7):request('invalid')
   self.assertEqual(request(code)[0],429)
  finally:gateway.shutdown();gateway.server_close();thread.join()
 def test_codes_expire_refresh_and_recover_only_same_key(self):
  one=issue_code(self.b,'办公室电脑');old=one['code'].replace(' ','')
  two=issue_code(self.b,'办公室电脑');code=two['code'].replace(' ','')
  with self.assertRaises(PermissionError):claim_code(self.b,old,self.xml,'one')
  reply=claim_code(self.b,code,self.xml,'one')
  self.assertEqual(claim_code(self.b,code,self.xml,'one'),reply)
  self.assertEqual(len(self.b.devices()),1)
  other=self.xml[:55]+('B' if self.xml[55]!='B' else 'C')+self.xml[56:]
  with self.assertRaises(PermissionError):claim_code(self.b,code,other,'one')
  with self.assertRaises(PermissionError):claim_code(self.b,code,self.xml,'one',two['expires'])
  for path in self.p.glob('*.json'):self.assertNotIn(code,path.read_text())
  self.assertEqual((self.p/'codes.secret').stat().st_mode&0o777,0o600)
 def test_rate_limit_survives_new_gateway_and_invalid_inputs(self):
  for _ in range(10):
   with self.assertRaises(PermissionError):claim_code(self.b,'invalid',self.xml,'same',100)
  with self.assertRaises(RateLimited):claim_code(self.b,'invalid',self.xml,'same',101)
  with self.assertRaises(PermissionError):claim_code(self.b,'invalid',self.xml,'same',161)
 def test_reusable_kit_has_no_pairing_credentials_or_code(self):
  one=issue_code(self.b,'new')
  packed=one['connection'][6:];decoded=json.loads(base64.urlsafe_b64decode(packed+'='*((-len(packed))%4)))
  self.assertEqual(decoded['code'],one['code'].replace(' ',''))
  self.assertEqual(decoded['pin'],self.cfg['pin'])
  output=self.root/'client.zip';export_client(self.b,output,'这台电脑')
  with zipfile.ZipFile(output) as z:
   self.assertNotIn('invitation.json',z.namelist());self.assertNotIn('pairing.bin',z.namelist())
   meta=json.loads(z.read('pairing-endpoint.json'));self.assertEqual(set(meta),{'url','pin','controller'})
   for name,digest in json.loads(z.read('manifest.json')).items():self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(),digest)
  before=output.read_bytes()
  with self.assertRaises(FileExistsError):export_client(self.b,output)
  self.assertEqual(output.read_bytes(),before)
  # Legacy invitation issuance must coexist with the short-code support files.
  self.kit('legacy')
  generic=self.root/'generic.zip';export_client(self.b,generic,generic=True)
  with zipfile.ZipFile(generic) as z:
   self.assertEqual(set(json.loads(z.read('pairing-endpoint.json'))),{'controller'})
 def test_rename_preserves_identity_and_connection(self):
  code=issue_code(self.b,'new')['code'].replace(' ','');reply=claim_code(self.b,code,self.xml,'test')
  before=dict(self.b.devices()[0]);runtime=Path(before['runtime']);config=(runtime/'config.json').read_bytes()
  self.b.rename(before['id'],'办公室电脑')
  self.assertEqual(self.b.device(before['id']),dict(before,name='办公室电脑'))
  self.assertEqual((runtime/'config.json').read_bytes(),config)
  self.assertEqual(claim_code(self.b,code,self.xml,'test'),reply)
  for invalid in ('','bad\nname','x'*101):
   with self.assertRaises(ValueError):self.b.rename(before['id'],invalid)

class SSHProfiles(unittest.TestCase):
 def test_terminal_command_uses_system_ssh_and_validates_input(self):
  from lanpilot.core import Bridge
  from lanpilot.ssh_profiles import rename
  with tempfile.TemporaryDirectory() as tmp:
   b=Bridge(Path(tmp));item=save(b,'示例服务器','work-server')
   with patch('lanpilot.ssh_profiles.subprocess.run') as run:
    open_terminal(b,item['id'])
    argv=run.call_args.args[0];command=Path(argv[-1]).read_text()
    self.assertIn('exec /usr/bin/ssh -- work-server',command)
    self.assertNotIn('StrictHostKeyChecking=no',command)
   self.assertEqual(profiles(b),[item])
   self.assertEqual(rename(b,item['id'],'备用服务器')['host'],'work-server')
   for host in ('-oProxyCommand=bad','x;bad','$(bad)','user@host','x\nbad'):
    with self.assertRaises(ValueError):save(b,'bad',host)
   with self.assertRaises(ValueError):save(b,'bad','host',user='$(bad)')
   with self.assertRaises(ValueError):save(b,'bad','host',port=0)
