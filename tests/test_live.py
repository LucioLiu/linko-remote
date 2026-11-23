import base64
from pathlib import Path
import secrets
import tempfile
import unittest
import uuid
from lanpilot.peer_hub import PeerHub
from lanpilot.live import validate_events


class LiveTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.now=1000
  self.hub=PeerHub(Path(self.tmp.name)/'hub.db',lambda:self.now)
  self.a,self.b,self.c=[secrets.token_urlsafe(32) for _ in range(3)]
  self.ids={t:self.call('register',t,name='Test PC',platform='windows',live_version=1)['id'] for t in (self.a,self.b,self.c)}
  self.call('availability',self.b,enabled=True)
  approval=self.call('connect',self.a,code=self.call('code',self.b)['code'])['approval']
  self.call('approve',self.b,approval=approval,allow=True)
 def tearDown(self):self.hub.close();self.tmp.cleanup()
 def call(self,op,token,**body):return self.hub.dispatch(op,token,body)
 def open(self):return self.call('live-open',self.a,peer=self.ids[self.b])['id']
 def frame(self,identifier,seq=1):
  return self.call('live-exchange',self.b,id=identifier,ack=0,frame={'seq':seq,'geometry':'0,0,1920,1080','jpeg_base64':base64.b64encode(b'\xff\xd8fixture\xff\xd9').decode()})
 def test_grant_is_required_in_both_directions_and_third_party_cannot_view(self):
  with self.assertRaises(PermissionError):self.call('live-open',self.b,peer=self.ids[self.a])
  ident=self.open();self.frame(ident)
  with self.assertRaises(PermissionError):self.call('live-view',self.c,id=ident)
  self.call('disconnect',self.b,peer=self.ids[self.a])
  with self.assertRaises(PermissionError):self.call('live-view',self.a,id=ident)
  self.assertEqual({},self.hub.live.sessions)
 def test_only_latest_frame_is_retained_and_screen_expires(self):
  ident=self.open();self.frame(ident);self.frame(ident,2)
  self.assertEqual(2,self.call('live-view',self.a,id=ident)['frame']['seq'])
  same=self.call('live-view',self.a,id=ident,after=2)
  self.assertTrue(same['unchanged']);self.assertIsNone(same['frame'])
  self.now+=2.1;self.assertIsNone(self.call('live-view',self.a,id=ident)['frame'])
 def test_independent_frame_channel_cannot_deliver_input_or_renew_viewer_lease(self):
  ident=self.open()
  frame={'seq':1,'geometry':'0,0,1920,1080','jpeg_base64':base64.b64encode(b'\xff\xd8frame\xff\xd9').decode()}
  with self.assertRaises(PermissionError):self.call('live-frame',self.a,id=ident,frame=frame)
  self.assertTrue(self.call('live-frame',self.b,id=ident,frame=frame)['active'])
  self.call('live-input',self.a,id=ident,seq=1,geometry=frame['geometry'],events=[{'type':'release'}])
  self.call('live-frame',self.b,id=ident,frame=frame)
  self.assertEqual(1,self.call('live-exchange',self.b,id=ident,ack=0)['inputs'][0]['seq'])
  self.now+=8.1
  self.assertFalse(self.call('live-frame',self.b,id=ident,frame=frame)['active'])
 def test_inputs_are_claimed_once_and_never_replayed_on_exchange_loss(self):
  ident=self.open();self.frame(ident)
  self.call('live-input',self.a,id=ident,seq=1,geometry='0,0,1920,1080',events=[{'type':'down','button':0,'x':100,'y':200}])
  first=self.call('live-exchange',self.b,id=ident,ack=0)
  self.assertEqual(1,first['inputs'][0]['seq'])
  self.assertEqual(first['inputs'][0]['seq'],self.call('live-exchange',self.b,id=ident,ack=0)['inputs'][0]['seq'])
  with self.assertRaises(ValueError):self.call('live-input',self.a,id=ident,seq=1,geometry='0,0,1920,1080',events=[{'type':'release'}])
  self.now+=3.1
  self.assertTrue(self.call('live-view',self.a,id=ident)['error'])
  self.assertFalse(self.call('live-exchange',self.b,id=ident,ack=0)['active'])
 def test_missing_viewer_expires_native_lease_and_no_more_input_is_delivered(self):
  ident=self.open();self.frame(ident);self.now+=7.5
  self.assertLessEqual(self.call('live-exchange',self.b,id=ident,ack=0)['lease_ms'],500)
  self.now+=.6
  self.assertFalse(self.call('live-exchange',self.b,id=ident,ack=0)['active'])
 def test_ack_cannot_skip_undelivered_input_and_stale_coordinates_are_rejected(self):
  ident=self.open();self.frame(ident)
  with self.assertRaises(ValueError):self.call('live-exchange',self.b,id=ident,ack=1)
  with self.assertRaises(ValueError):self.call('live-input',self.a,id=ident,seq=1,geometry='0,0,800,600',events=[{'type':'release'}])
  self.now+=1.6
  with self.assertRaises(ValueError):self.call('live-input',self.a,id=ident,seq=1,geometry='0,0,1920,1080',events=[{'type':'release'}])
 def test_live_and_guarded_actions_cannot_control_target_together(self):
  ident=self.open()
  with self.assertRaises(ValueError):self.call('submit',self.a,peer=self.ids[self.b],id=uuid.uuid4().hex,request={'action':'observe'})
  self.call('live-close',self.a,id=ident)
  self.call('submit',self.a,peer=self.ids[self.b],id=uuid.uuid4().hex,request={'action':'observe'})
  with self.assertRaises(ValueError):self.open()
 def test_expired_snapshot_does_not_block_live_open_without_recovery(self):
  self.call('submit',self.a,peer=self.ids[self.b],id=uuid.uuid4().hex,request={'action':'observe'})
  self.call('poll',self.b)
  self.now+=46
  self.assertTrue(self.open())
 def test_media_timeout_is_visible_to_the_original_viewer_and_frees_the_target(self):
  from lanpilot.live import MEDIA_FAILURES
  ident=self.open()
  self.call('live-close',self.b,id=ident,reason='first_frame_timeout')
  self.assertEqual(MEDIA_FAILURES['first_frame_timeout'],self.call('live-view',self.a,id=ident)['error'])
  with self.assertRaises(PermissionError):self.call('live-view',self.c,id=ident)
  self.assertNotEqual(ident,self.open())
  self.now+=12
  with self.assertRaises(PermissionError):self.call('live-view',self.a,id=ident)
 def test_invalid_input_and_oversized_frames_are_rejected(self):
  for e in [[{'type':'move','x':-1,'y':0}],[{'type':'keydown','code':'arbitrary'}],[{'type':'text','text':'x'*501}]]:
   with self.assertRaises(ValueError):validate_events(e)
  ident=self.open()
  with self.assertRaises(ValueError):self.call('live-exchange',self.b,id=ident,frame={'seq':1,'geometry':'0,0,1,1','jpeg_base64':'x'*1_400_001})
 def test_lost_read_only_poll_can_expire_but_input_cannot_be_falsely_cancelled(self):
  for action in ['observe','click']:
   ident=uuid.uuid4().hex;request={'action':action}
   if action=='click':request.update(frame_id='a'*32,x=0,y=0,button='left')
   self.call('submit',self.a,peer=self.ids[self.b],id=ident,request=request);self.call('poll',self.b)
   self.now+=46
   recovered=self.call('recover',self.a,id=ident)
   self.assertEqual('complete' if action=='observe' else 'unconfirmed',recovered['state'])
   if action=='observe':
    self.assertEqual('SCREEN_CAPTURE_TIMED_OUT',recovered['result']['error'])
    self.assertTrue(self.call('result',self.b,id=ident,result={'request_id':ident,'ok':True,'input_attempted':False})['saved'])

class ReadRecoveryTransportTests(unittest.TestCase):
 def test_only_reads_reconnect_after_tls_eof(self):
  from unittest.mock import Mock
  from lanpilot.peer_client import PinnedHub
  for operation in ('recover','state','live-view','submit','live-input'):
   client=object.__new__(PinnedHub)
   client._call=Mock(side_effect=[OSError('TLS EOF'),{'complete':True}])
   if operation in ('submit','live-input'):
    with self.assertRaises(OSError):client.call(operation,{})
    self.assertEqual(1,client._call.call_count)
   else:
    self.assertTrue(client.call(operation,{})['complete'])
    self.assertEqual(2,client._call.call_count)

class DurableLiveAcceptanceTests(unittest.TestCase):
 def test_pause_while_exchange_is_in_flight_never_renews_the_lease(self):
  import threading
  from types import SimpleNamespace
  from lanpilot.live_agent import LiveAgent
  ident='b'*32;writes=[]
  client=SimpleNamespace(config={'endpoint':{},'credential':'fixture'},incoming=True,
                         stop_event=threading.Event(),agent=SimpleNamespace(workspace=Path('.')))
  class Hub:
   def call(self,operation,body=None):
    if operation=='live-agent-state':return {'sessions':[{'id':ident}]}
    if operation=='live-exchange':
     client.incoming=False
     client.stop_event.set()
     return {'active':True,'lease_ms':2500,'inputs':[]}
    return {'closed':True}
  agent=LiveAgent(client,lambda *_:Hub(),lambda _,value:writes.append(value))
  agent.read=lambda _:{}
  agent.run()
  self.assertEqual([{'id':ident,'expires':0}],writes)

 def test_first_frame_has_more_time_without_extending_the_active_frame_gap(self):
  from types import SimpleNamespace
  from unittest.mock import patch
  from lanpilot.live_agent import LiveAgent
  for first_frame_at,expected_reason,expected_at in [(None,'first_frame_timeout',16),(10,'frame_gap',19)]:
   with self.subTest(first_frame_at=first_frame_at):
    now=0;closed=[];writes=[];ident='b'*32
    class Stop:
     def is_set(self):return bool(closed) or now>20
     def wait(self,_):
      nonlocal now
      now+=1
    class Hub:
     def call(self,operation,body=None):
      if operation=='live-agent-state':return {'sessions':[{'id':ident}]}
      if operation=='live-exchange':
       if now==first_frame_at:agent.last_frame=1;agent.media_at=now
       return {'active':True,'lease_ms':2500,'inputs':[]}
      closed.append((now,body['reason']))
      return {'closed':True}
    client=SimpleNamespace(config={'endpoint':{},'credential':'fixture'},incoming=True,
                           stop_event=Stop(),agent=SimpleNamespace(workspace=Path('.')))
    agent=LiveAgent(client,lambda *_:Hub(),lambda _,value:writes.append(value))
    agent.read=lambda _:{}
    with patch('lanpilot.live_agent.time.monotonic',lambda:now):agent.run()
    self.assertEqual([(expected_at,expected_reason)],closed)
    self.assertEqual(0,writes[-1]['expires'])
 def test_lost_delivery_response_and_restart_do_not_repeat_input(self):
  from lanpilot.live_agent import LiveAgent
  from lanpilot.peer_client import atomic_json
  with tempfile.TemporaryDirectory() as folder:
   def agent():
    a=object.__new__(LiveAgent);a.root=Path(folder);a.known=set();a.write=atomic_json;return a
   ident='a'*32;batch={'seq':1,'geometry':'0,0,100,100','events':[{'type':'down','button':0,'x':1,'y':1}],'valid_for_ms':2000}
   a=agent();self.assertTrue(a.accept(ident,batch,.1))
   path=next(Path(folder).glob('*.live-input.json'));original=path.read_bytes()
   self.assertFalse(a.accept(ident,batch,.1));self.assertEqual(original,path.read_bytes())
   path.rename(path.with_suffix('.executing'))
   self.assertFalse(agent().accept(ident,batch,.1))
   self.assertEqual([],list(Path(folder).glob('*.live-input.json')))
 def test_slow_frame_upload_does_not_block_input_lease_and_disconnect_releases_it(self):
  import threading,time,json
  from types import SimpleNamespace
  from lanpilot.live_agent import LiveAgent
  from lanpilot.peer_client import atomic_json
  with tempfile.TemporaryDirectory() as folder:
   ident='b'*32;blocked=threading.Event();unblock=threading.Event();renewed=threading.Event();leases=[]
   stop=threading.Event();root=Path(folder)
   client=SimpleNamespace(config={'endpoint':{},'credential':'fixture'},agent=SimpleNamespace(workspace=root),incoming=True,stop_event=stop)
   class Control:
    def call(self,operation,body=None):
     if operation=='live-agent-state':return {'sessions':[{'id':ident}]}
     if operation=='live-exchange':return {'active':True,'lease_ms':2500,'inputs':[]}
     return {'closed':True}
   class Frames:
    def call(self,operation,body):
     blocked.set();unblock.wait(2);return {'active':True}
   connections=iter([Control(),Frames()])
   def write(path,value):
    atomic_json(path,value)
    if path.name=='live-control.json':
     leases.append(value['expires'])
     if blocked.is_set() and value['expires']>time.time():renewed.set()
   atomic_json(root/'live-frame.json',{'id':ident,'seq':1,'created':time.time(),'geometry':'0,0,100,100','jpeg_base64':'fixture'})
   agent=LiveAgent(client,lambda *args:next(connections),write)
   worker=threading.Thread(target=agent.run);media=threading.Thread(target=agent.frames)
   worker.start();media.start()
   try:
    self.assertTrue(blocked.wait(1));self.assertTrue(renewed.wait(1))
    client.incoming=False
    deadline=time.monotonic()+1
    while agent.current and time.monotonic()<deadline:time.sleep(.01)
    self.assertIsNone(agent.current)
    self.assertEqual(0,json.loads((root/'live-control.json').read_text())['expires'])
   finally:
    stop.set();unblock.set();worker.join(2);media.join(2)

class ServiceMigrationTests(unittest.TestCase):
 def test_verified_release_migrates_only_retired_endpoint_and_keeps_identity(self):
  import json
  from lanpilot.peer_client import DesktopClient, LEGACY_PREVIEW_ENDPOINT, atomic_json
  class Agent:
   def __init__(self,home):self.workspace=Path(home)/'worker';self.workspace.mkdir()
  class Hub:
   def __init__(self,*args):pass
  with tempfile.TemporaryDirectory() as folder:
   for name,old in [('retired',LEGACY_PREVIEW_ENDPOINT),('custom',{'url':'https://custom.example','pin':'a'*64})]:
    home=Path(folder)/name;home.mkdir()
    config={'credential':secrets.token_urlsafe(32),'name':'Saved PC','ssh':[{'name':'Saved SSH'}],'endpoint':old}
    atomic_json(home/'client.json',config)
    endpoint={'url':'https://new.example','pin':'b'*64}
    client=DesktopClient(home,endpoint,Hub,Agent)
    self.assertEqual(config['credential'],client.config['credential']);self.assertEqual(config['name'],client.config['name']);self.assertEqual(config['ssh'],client.config['ssh'])
    self.assertEqual(endpoint if name=='retired' else old,client.config['endpoint'])
    if name=='retired':self.assertEqual(config,json.loads((home/'client-before-relay-migration.json').read_text()))
 def test_completed_receipt_body_can_be_removed_without_losing_late_ack(self):
  import json,hashlib
  with tempfile.TemporaryDirectory() as folder:
   hub=PeerHub(Path(folder)/'hub.db');a,b=[secrets.token_urlsafe(32)for _ in range(2)]
   for t in [a,b]:hub.dispatch('register',t,{'name':'Test PC','platform':'windows'})
   target=hub.dispatch('state',b,{})['self']['id'];hub.dispatch('availability',b,{'enabled':True})
   approval=hub.dispatch('connect',a,{'code':hub.dispatch('code',b,{})['code']})['approval'];hub.dispatch('approve',b,{'approval':approval,'allow':True})
   ident=uuid.uuid4().hex;hub.dispatch('submit',a,{'peer':target,'id':ident,'request':{'action':'observe'}});hub.dispatch('poll',b,{})
   receipt={'request_id':ident,'ok':True,'input_attempted':False,'result':{'jpeg_base64':'private-screen'}}
   hub.dispatch('result',b,{'id':ident,'result':receipt})
   canonical=json.dumps(receipt,sort_keys=True,separators=(',',':'));hub.db.execute('INSERT INTO migrated_receipts VALUES(?,?)',(ident,hashlib.sha256(canonical.encode()).hexdigest()))
   stripped={k:v for k,v in receipt.items()if k!='result'};hub.db.execute('UPDATE commands SET result=? WHERE id=?',(json.dumps(stripped),ident))
   self.assertTrue(hub.dispatch('result',b,{'id':ident,'result':receipt})['saved'])
   self.assertNotIn('private-screen',hub.db.execute('SELECT result FROM commands WHERE id=?',(ident,)).fetchone()[0]);hub.close()

class BackgroundViewTests(unittest.TestCase):
 def test_frames_keep_arriving_without_browser_timer_calls(self):
  import threading,time
  from lanpilot.live_view import LiveView
  class Hub:
   seq=0
   def call(self,operation,body):
    self.seq+=1
    return {'id':body['id'],'frame':{'seq':self.seq,'geometry':'0,0,100,100','jpeg_base64':'private-image'},'ack':0,'error':'','frame_age_ms':0}
  view=LiveView(Hub(),'a'*32,threading.Event());view.start()
  try:
   time.sleep(.22)
   first=view.snapshot();self.assertGreaterEqual(first['frame']['seq'],3)
   self.assertNotIn('jpeg_base64',first['frame'])
   time.sleep(.13)
   self.assertGreater(view.snapshot()['frame']['seq'],first['frame']['seq'])
  finally:view.close();view.thread.join(timeout=1)
 def test_stale_frame_is_never_usable_for_input(self):
  import threading
  from lanpilot.live_view import LiveView
  view=LiveView(None,'a'*32,threading.Event())
  view.publish({'id':view.id,'frame':{'seq':1,'geometry':'0,0,100,100','jpeg_base64':'private-image'},'ack':4,'error':'','frame_age_ms':1600})
  self.assertIsNone(view.snapshot()['frame']);self.assertEqual(4,view.snapshot()['ack'])
 def test_delayed_frame_cannot_be_used_for_input_on_arrival(self):
  import threading
  from lanpilot.live_view import LiveView
  view=LiveView(None,'a'*32,threading.Event())
  frame={'seq':1,'geometry':'0,0,100,100','jpeg_base64':'fixture'}
  value={'id':view.id,'frame':frame,'ack':0,'error':'','frame_age_ms':350}
  view.publish(value,roundtrip=1.3)
  self.assertIsNone(view.snapshot()['frame'])
  view.publish(dict(value,frame=dict(frame,seq=2)),roundtrip=.1)
  self.assertEqual(2,view.snapshot()['frame']['seq'])

class LocalFrameFeedTests(unittest.TestCase):
 def test_feed_requires_local_session_and_delivers_jpeg_without_json_timer(self):
  import threading,http.server,http.client,base64
  from lanpilot.peer_client import LocalHandler
  from lanpilot.live_view import LiveView
  jpeg=b'\xff\xd8local-frame\xff\xd9';ident='d'*32
  stop=threading.Event();view=LiveView(None,ident,stop)
  view.publish({'id':ident,'frame':{'seq':1,'geometry':'0,0,100,100','jpeg_base64':base64.b64encode(jpeg).decode()},'ack':0,'error':'','frame_age_ms':0})
  class Client:pass
  client=Client();client.live_view=view;client.stop_event=stop
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.daemon_threads=True
  server.client=client;server.nonce=secrets.token_urlsafe(32);server.authority='127.0.0.1:'+str(server.server_port)
  worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
  try:
   for nonce,origin in [('wrong',None),(server.nonce,'https://untrusted.example')]:
    c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2);c.request('GET','/live-feed/'+ident+'?session='+nonce,headers={'Origin':origin} if origin else {});r=c.getresponse();self.assertEqual(403,r.status);r.read();c.close()
   c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2);c.request('GET','/live-feed/'+ident+'?session='+server.nonce)
   r=c.getresponse();self.assertEqual(200,r.status);self.assertIn('multipart/x-mixed-replace',r.getheader('Content-Type'))
   self.assertEqual(b'--linko-frame\r\n',r.fp.readline())
   while r.fp.readline()!=b'\r\n':pass
   self.assertEqual(jpeg,r.fp.read(len(jpeg)));r.close();c.close()
  finally:
   stop.set();view.close();server.shutdown();server.server_close();worker.join(timeout=2)
