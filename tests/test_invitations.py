import base64, hashlib, http.client, io, json, ssl, subprocess, tempfile, threading, time, unittest, zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from lanpilot.core import Bridge
from lanpilot.invitations import claim,directory,issue,public_key
from lanpilot.invite_gateway import Gateway
from relay import init_runtime
from lanpilot.broker import start_listener
from lanpilot.transport import LocalClient

class Invitations(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.b=Bridge(self.root/'home');self.p=directory(self.b)
  with redirect_stdout(io.StringIO()):init_runtime(self.p,'127.0.0.1',33333)
  cfg=json.loads((self.p/'config.json').read_text());(self.p/'config.json').unlink()
  self.cfg={'pin':cfg['pin'],'url':'https://127.0.0.1:44444'}
  (self.p/'endpoint.json').write_text(json.dumps(self.cfg))
  key=self.root/'private.pem';subprocess.run(['openssl','genrsa','-out',str(key),'2048'],check=True,capture_output=True)
  modulus=bytes.fromhex(subprocess.check_output(['openssl','rsa','-in',str(key),'-noout','-modulus']).decode().strip().split('=')[1])
  self.xml='<RSAKeyValue><Modulus>'+base64.b64encode(modulus).decode()+'</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>'
 def kit(self,name):
  path=self.root/(name+'.cmd');issue(self.b,name,path)
  header,body=path.read_bytes().split(b'\r\n::PAYLOAD\r\n');self.assertIn(b'Claim-Invitation.ps1',header)
  with zipfile.ZipFile(io.BytesIO(base64.b64decode(body))) as z:
   desc=json.loads(z.read('invitation.json'));checksums=json.loads(z.read('manifest.json'))
   for f,digest in checksums.items():self.assertEqual(hashlib.sha256(z.read(f)).hexdigest(),digest)
   self.assertNotIn('pairing.bin',z.namelist());self.assertNotIn('server.key',z.namelist())
  self.assertNotIn(desc['token'],str([x.name for x in self.p.iterdir()]));return desc
 def test_one_key_concurrent_recovery_and_expiry(self):
  k=self.kit('new');t=k['token']
  with ThreadPoolExecutor(3) as pool:claims=list(pool.map(lambda _:claim(self.b,t,self.xml),range(3)))
  self.assertEqual(claims,[claims[0]]*3);self.assertEqual(len(self.b.devices()),1)
  with self.assertRaises(PermissionError):claim(self.b,t,self.xml,k['expires'])
  with self.assertRaises(PermissionError):claim(self.b,t, self.xml[:55]+('B' if self.xml[55]!='B' else 'C')+self.xml[56:])
  d=self.b.devices()[0];rt=Path(d['runtime']);cfg=json.loads((rt/'config.json').read_text())
  cipher=self.root/'encrypted';cipher.write_bytes(base64.b64decode(claims[0]['pairing']))
  plaintext=json.loads(subprocess.check_output(['openssl','pkeyutl','-decrypt','-inkey',str(self.root/'private.pem'),'-pkeyopt','rsa_padding_mode:oaep','-in',str(cipher)]))
  self.assertEqual(plaintext,{'token':cfg['device'],'pin':cfg['pin'],'url':cfg['url']});self.assertEqual(cfg['pin'],self.cfg['pin'])
  before=self.b.registry.read_bytes();second=self.kit('duplicate-key')
  with self.assertRaisesRegex(ValueError,'already paired'):claim(self.b,second['token'],self.xml)
  self.assertEqual(before,self.b.registry.read_bytes())
 def test_pinned_http_claim_and_admin_denial(self):
  g=Gateway(self.b,'127.0.0.1',0);thread=threading.Thread(target=g.serve_forever,daemon=True);thread.start()
  try:
   def call(method,path,token,body=None):
    cx=http.client.HTTPSConnection(*g.server_address,context=ssl._create_unverified_context(),timeout=4);cx.connect()
    self.assertEqual(hashlib.sha256(cx.sock.getpeercert(True)).hexdigest(),self.cfg['pin'])
    try:
     cx.request(method,path,body,headers={'Authorization':'Bearer '+token});resp=cx.getresponse();return resp.status,resp.read()
    finally:cx.close()
   k=self.kit('web-test');payload=json.dumps({'public_key':self.xml})
   self.assertEqual(call('POST','/claim','A'*43,payload)[0],403)
   status,body=call('POST','/claim',k['token'],payload);self.assertEqual(status,200);reply=json.loads(body)
   cfg=json.loads((Path(self.b.device(reply['device_id'])['runtime'])/'config.json').read_text())
   self.assertEqual(call('POST','/submit',cfg['admin'],'{}')[0],404)
   self.assertEqual(call('GET','/poll',cfg['admin'])[0],401)
   self.assertEqual(g.find(cfg['device'])['port'],cfg['port'])
   rt=Path(self.b.device(reply['device_id'])['runtime']);upstream=start_listener(rt,cfg)
   try:
    admin=LocalClient(rt);self.addCleanup(admin.connection.close);rid='c'*32;admin.call('/submit',{'id':rid,'request':{'action':'observe'}})
    status,body=call('GET','/poll?session='+'b'*32,cfg['device']);self.assertEqual(status,200);self.assertIn('request',json.loads(body))
    self.assertEqual(call('POST','/result',cfg['device'],json.dumps({'request_id':rid,'ok':True,'result':{}}))[0],200)
    self.assertTrue(admin.call('/result/'+rid+'?wait=0')['ok'])
   finally:upstream.shutdown();upstream.server_close()
  finally:g.shutdown();g.server_close();thread.join()
