import unittest,tempfile,json,subprocess,sys
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lanpilot.discovery import request,response,verify
from lanpilot.core import Bridge
class DiscoveryTests(unittest.TestCase):
 def test_authenticated_discovery_and_forgery_rejection(self):
  cfg={'device':'test-only-device-key','port':18769};q=request(cfg['device']);r=response(cfg,q,'192.0.2.203')
  self.assertTrue(verify(cfg['device'],q['nonce'],r))
  self.assertFalse(verify('wrong-key',q['nonce'],r));self.assertFalse(verify(cfg['device'],'0'*32,r))
  r['url']='https://192.0.2.99:18769';self.assertFalse(verify(cfg['device'],q['nonce'],r))
  q['mac']='0'*64;self.assertIsNone(response(cfg,q,'192.0.2.203'))
class BridgeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.rt=self.root/'runtime';self.rt.mkdir();(self.rt/'config.json').write_text(json.dumps({'url':'https://127.0.0.1:18769','admin':'test-only-admin','device':'test-only-key','pin':'a'*64,'port':18769}))
  self.b=Bridge(self.root/'app');self.d=self.b.adopt('Test',self.rt)
 def tearDown(self):self.tmp.cleanup()
 def test_registry_does_not_copy_credentials(self):
  self.assertNotIn('test-only-admin',self.b.registry.read_text());self.assertEqual(self.b.registry.stat().st_mode&0o777,0o600)
  with self.assertRaises(ValueError):self.b.adopt('again',self.rt)
 def test_multiple_devices_reject_identity_and_port_collisions(self):
  other=self.root/'other';other.mkdir();cfg=json.loads((self.rt/'config.json').read_text())
  cfg['device']='second-key';(other/'config.json').write_text(json.dumps(cfg))
  with self.assertRaisesRegex(ValueError,'端口') :self.b.adopt('Second',other)
  cfg['port']=18767;cfg['device']='test-only-key';(other/'config.json').write_text(json.dumps(cfg))
  with self.assertRaisesRegex(ValueError,'身份'):self.b.adopt('Second',other)
  cfg['device']='second-key';(other/'config.json').write_text(json.dumps(cfg))
  second=self.b.adopt('Second',other)
  self.assertNotEqual(second['id'],self.d['id']);self.assertEqual(len(self.b.devices()),2)
  with patch('lanpilot.core.Client') as C:
   C.return_value.call.return_value={'ok':True,'result':{}}
   self.b.request(second['id'],{'action':'observe'})
   C.assert_called_once_with(other.resolve())
 def test_lost_submission_never_replays(self):
  with patch('lanpilot.core.Client') as C:
   C.return_value.call.side_effect=ConnectionError('lost')
   r=self.b.request('Test',{'action':'observe'})
   self.assertEqual(r['state'],'unconfirmed');self.assertEqual(C.return_value.call.call_count,1)
 def test_recover_only_reads_same_id(self):
  rid='b'*32
  with patch('lanpilot.core.Client') as C:
   C.return_value.call.return_value={'ok':True,'request_id':rid,'result':{}}
   self.b.request('Test',None,rid)
   C.return_value.call.assert_called_once_with('/result/'+rid+'?wait=20')
 def test_default_left_click_and_invalid_input_rejected_locally(self):
  with patch('lanpilot.core.Client') as C:
   C.return_value.call.return_value={'ok':True,'result':{}}
   self.b.request('Test',{'action':'click','frame_id':'a'*32,'x':1,'y':1})
   self.assertEqual(C.return_value.call.call_args_list[0].args[1]['request']['button'],'left')
  with patch('lanpilot.core.Client') as C:
   with self.assertRaises(ValueError):self.b.request('Test',{'action':'text','text':'unbound'})
   C.assert_not_called()
 def test_unsupported_key_is_rejected_before_submission(self):
  with patch('lanpilot.core.Client') as C:
   with self.assertRaises(ValueError):self.b.request('Test',{'action':'key','frame_id':'a'*32,'keys':['WIN','R']})
   C.assert_not_called()
 def test_unknown_device_does_not_connect(self):
  with patch('lanpilot.core.Client') as C:
   with self.assertRaises(ValueError):self.b.request('No',{'action':'observe'})
   C.assert_not_called()
class TransportTests(unittest.TestCase):
 def test_pin_mismatch_closes_before_request_headers(self):
  from lanpilot.transport import PinnedConnection
  from unittest.mock import MagicMock
  import ssl
  c=PinnedConnection(18769,'0'*64);sock=MagicMock();sock.getpeercert.return_value=b'wrong-peer'
  def connected():c.sock=sock
  with patch('http.client.HTTPSConnection.connect',side_effect=connected):
   with self.assertRaises(ssl.SSLError):c.request('GET','/health',headers={'Authorization':'Bearer test-only-secret'})
  sock.sendall.assert_not_called();sock.close.assert_called_once()

class MCPTests(unittest.TestCase):
 def test_stdio_initialize_list_and_bad_json(self):
  lines=['bad',json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05'}}),json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list'})]
  p=subprocess.run([sys.executable,'-m','lanpilot.mcp'],input='\n'.join(lines)+'\n',text=True,capture_output=True,check=True,cwd=ROOT)
  replies=[json.loads(x) for x in p.stdout.splitlines()];self.assertEqual(len(replies),3);self.assertEqual(replies[1]['result']['serverInfo']['name'],'lanpilot');self.assertEqual({t['name'] for t in replies[2]['result']['tools']},{'list_devices','observe','act','recover','stage_inputs','run_job'})
if __name__=='__main__':unittest.main()
