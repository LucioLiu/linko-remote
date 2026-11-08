import importlib.util,sys,time,unittest,threading
from unittest.mock import Mock,patch
import http.client,http.server,json
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
import relay
class RelayTests(unittest.TestCase):
 def test_one_pending_and_no_duplicate(self):
  s=relay.State('a','b');rid='a'*32;s.submit(rid,{'action':'observe'})
  with self.assertRaises(ValueError):s.submit('b'*32,{'action':'observe'})
  x=s.poll();self.assertEqual(x['id'],rid)
  self.assertGreater(x['valid_for_ms'],0)
  self.assertLessEqual(x['valid_for_ms'],45000)
  # Polling delivery does not authorize re-execution through submit.
  with self.assertRaises(ValueError):s.submit(rid,{'action':'observe'})
 def test_replaced_poll_session_cannot_consume_new_request(self):
  s=relay.State('a','b');out={}
  old=threading.Thread(target=lambda:out.update(old=s.poll('1'*32)));old.start()
  while s.active_session!='1'*32:time.sleep(.001)
  new=threading.Thread(target=lambda:out.update(new=s.poll('2'*32)));new.start()
  while s.active_session!='2'*32:time.sleep(.001)
  s.submit('a'*32,{'action':'status'})
  old.join(16);new.join(16)
  self.assertIsNone(out['old']);self.assertEqual(out['new']['id'],'a'*32)
 def test_results_idempotent_not_conflicting(self):
  s=relay.State('a','b');rid='a'*32;s.submit(rid,{'action':'status'})
  result={'request_id':rid,'ok':True};s.result(rid,result);s.result(rid,result)
  with self.assertRaises(ValueError):s.result(rid,{'request_id':rid,'ok':False})
  with self.assertRaises(ValueError):s.result('b'*32,{'request_id':'b'*32})
 def test_expired_not_delivered(self):
  s=relay.State('a','b');rid='a'*32;s.submit(rid,{'action':'status'})
  s.requests[rid]['expires']=time.time()-1
  self.assertIsNone(s.poll());self.assertFalse(s.results[rid]['input_attempted'])
 def test_reject_unknown_action(self):
  s=relay.State('a','b')
  with self.assertRaises(ValueError):s.submit('a'*32,{'action':'exec','command':'anything'})
 def test_result_wait_wakes_without_poll_interval(self):
  s=relay.State('a','b');rid='c'*32;s.submit(rid,{'action':'status'})
  result={'request_id':rid,'ok':True}
  timer=threading.Timer(.04,lambda:s.result(rid,result));timer.start()
  try:
   self.assertEqual(s.wait_result(rid,1),result)
   self.assertEqual(s.wait_result(rid,0),result)
  finally:timer.join()
 def test_result_wait_timeout_does_not_resubmit(self):
  s=relay.State('a','b');rid='d'*32;s.submit(rid,{'action':'status'})
  self.assertIsNone(s.wait_result(rid,.01))
  self.assertEqual(s.pending.qsize(),1)
  with self.assertRaises(ValueError):s.wait_result('e'*32,0)
  with self.assertRaises(ValueError):s.wait_result(rid,21)
 def test_client_transport_failure_is_not_retried(self):
  c=relay.Client.__new__(relay.Client);c.cfg={'admin':'test'}
  c.last_completed=None
  c.connection=Mock();c.connection.request.side_effect=ConnectionResetError('test')
  with self.assertRaises(ConnectionResetError):c.call('/submit',{'id':'a'*32})
  self.assertEqual(c.connection.request.call_count,1)
  c.connection.close.assert_called_once()
 def test_idle_connection_retired_before_new_request(self):
  c=relay.Client.__new__(relay.Client);c.cfg={'admin':'test'};c.last_completed=time.monotonic()-31
  c.connection=Mock();r=c.connection.getresponse.return_value;r.status=200;r.read.return_value=b'{"ok":true}'
  self.assertEqual(c.call('/health'),{'ok':True})
  self.assertEqual(c.connection.method_calls[0][0],'close')
  self.assertEqual(c.connection.request.call_count,1)
 def test_http_keepalive_preserves_response_boundaries(self):
  state=relay.State('admin-test','device-test')
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),relay.handler(state))
  server.daemon_threads=True
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  c=http.client.HTTPConnection(*server.server_address,timeout=2)
  try:
   c.request('GET','/health',headers={'Authorization':'Bearer admin-test'})
   response=c.getresponse();self.assertEqual(response.status,200);json.loads(response.read())
   sock=c.sock;self.assertIsNotNone(sock)
   c.request('GET','/health',headers={'Authorization':'Bearer admin-test'})
   response=c.getresponse();self.assertEqual(response.status,200);json.loads(response.read())
   self.assertIs(c.sock,sock)
   c.request('POST','/submit',body='{}',headers={'Authorization':'Bearer invalid','Content-Length':'2'})
   response=c.getresponse();self.assertEqual(response.status,401);response.read()
   self.assertIsNone(c.sock)
  finally:
   c.close();server.shutdown();server.server_close();thread.join()
 def test_restart_retains_receipt_without_replaying_input(self):
  with tempfile.TemporaryDirectory() as tmp:
   journal=Path(tmp)/'journal';s=relay.State('a','b',journal);rid='1'*32
   s.submit(rid,{'action':'text','frame_id':'f'*32,'text':'test'});s.poll()
   result={'request_id':rid,'ok':True,'input_attempted':True}
   s.result(rid,result)
   restored=relay.State('a','b',journal)
   self.assertEqual(restored.wait_result(rid,0),result)
   self.assertEqual(restored.pending.qsize(),0)
   with self.assertRaises(ValueError):restored.submit(rid,{'action':'status'})
   self.assertEqual((journal/(rid+'.json')).stat().st_mode&0o777,0o600)
 def test_restart_unconfirmed_delivery_accepts_late_receipt_never_requeues(self):
  with tempfile.TemporaryDirectory() as tmp:
   journal=Path(tmp)/'journal';s=relay.State('a','b',journal);rid='2'*32
   s.submit(rid,{'action':'key','frame_id':'f'*32,'keys':['ENTER']});s.poll()
   restored=relay.State('a','b',journal)
   self.assertIsNone(restored.wait_result(rid,0));self.assertEqual(restored.pending.qsize(),0)
   result={'request_id':rid,'ok':False,'input_attempted':True}
   restored.result(rid,result);self.assertEqual(restored.wait_result(rid,0),result)
 def test_restart_undelivered_request_fails_closed(self):
  with tempfile.TemporaryDirectory() as tmp:
   journal=Path(tmp)/'journal';s=relay.State('a','b',journal);rid='3'*32
   s.submit(rid,{'action':'key','frame_id':'f'*32,'keys':['ENTER']})
   restored=relay.State('a','b',journal)
   self.assertEqual(restored.pending.qsize(),0)
   result=restored.wait_result(rid,0)
   self.assertFalse(result['ok']);self.assertFalse(result['input_attempted'])
 def test_legacy_screenshot_is_loaded_only_for_requested_id(self):
  with tempfile.TemporaryDirectory() as tmp:
   journal=Path(tmp);rid='4'*32
   req={'action':'text','text':'quoted JSON: }, "delivered":true, "result": {"x":1}', 'expires':time.time()+45}
   receipt={'request_id':rid,'ok':True,'result':{'png_base64':'A'*1_000_000}}
   path=journal/(rid+'.json')
   path.write_text(json.dumps({'request':req,'delivered':True,'result':receipt,'timings':{}}))
   with patch('relay.json.load',side_effect=AssertionError('Parsed archived screenshot during startup')):
    restored=relay.State('a','b',journal)
   self.assertFalse(restored.requests or restored.results or restored.delivered or restored.timings)
   with self.assertRaisesRegex(ValueError,'Duplicate'):restored.submit(rid,{'action':'status'})
   self.assertEqual(restored.wait_result(rid,0),receipt)
   restored.result(rid,receipt)
   with self.assertRaisesRegex(ValueError,'Conflicting'):restored.result(rid,{'request_id':rid,'ok':False})

 def test_disk_receipt_write_failure_and_competing_retries(self):
  with tempfile.TemporaryDirectory() as tmp:
   journal=Path(tmp);s=relay.State('a','b',journal);rid='5'*32
   s.submit(rid,{'action':'status'});s.poll()
   good={'request_id':rid,'ok':True};other={'request_id':rid,'ok':False}
   with patch('relay.os.replace',side_effect=OSError('disk unavailable')):
    with self.assertRaises(OSError):s.result(rid,good)
   self.assertIsNone(s.wait_result(rid,0))
   self.assertIsNone(json.loads((journal/(rid+'.json')).read_text())['result'])
   barrier=threading.Barrier(3);accepted=[];conflicts=[];received=[]
   def send(value):
    barrier.wait()
    try:s.result(rid,value);accepted.append(value)
    except ValueError as e:conflicts.append(str(e))
   waiter=threading.Thread(target=lambda:received.append(s.wait_result(rid,2)))
   threads=[threading.Thread(target=send,args=(value,)) for value in (good,other)]
   waiter.start()
   for th in threads:th.start()
   barrier.wait()
   for th in threads+[waiter]:th.join(3)
   self.assertEqual(conflicts,['Conflicting result']);self.assertEqual(len(accepted),1)
   self.assertEqual(received,accepted)
   self.assertEqual(relay.State('a','b',journal).wait_result(rid,0),accepted[0])
   self.assertFalse(s.requests or s.results or s.timings or s.delivered)

 def test_legacy_undelivered_input_never_reenters_queue(self):
  with tempfile.TemporaryDirectory() as tmp:
   journal=Path(tmp);rid='6'*32
   req={'action':'text','text':'\u0001'*2000+' "result": {"note":"partial"}','expires':time.time()+45}
   (journal/(rid+'.json')).write_text(json.dumps({'request':req,'delivered':False,'result':None}))
   restored=relay.State('a','b',journal)
   self.assertEqual(restored.wait_result(rid,0)['error'],'BROKER_RESTART_BEFORE_DELIVERY_OBSERVE_AGAIN')
   self.assertTrue(restored.pending.empty())

if __name__=='__main__':unittest.main()
