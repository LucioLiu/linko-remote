#!/usr/bin/env python3
"""Temporary TLS relay. Windows polls outbound; only narrow desktop actions allowed."""
import argparse,base64,hashlib,hmac,http.server,http.client,json,os,queue,re,secrets,socket,ssl,subprocess,sys,threading,time,urllib.request,uuid
from urllib.parse import urlsplit,parse_qs
from pathlib import Path
from desktop import make_request,save_result

def journal_has_receipt(path):
 """Skip archived screenshot bytes, including in the original request-first format."""
 with path.open(encoding='utf-8') as f:
  prefix=f.read(1024)
  if re.match(r'\s*\{\s*"result"\s*:\s*\{',prefix):return True
  request=re.match(r'\s*\{\s*"request"\s*:\s*',prefix)
  if not request:return False
  # Decode the actual request object: its typed text may itself contain JSON.
  for size in (0,16384):
   if size:prefix+=f.read(size)
   try:_,end=json.JSONDecoder().raw_decode(prefix,request.end())
   except ValueError:continue
   if re.match(r'\s*,\s*"delivered"\s*:\s*(true|false)\s*,\s*"result"\s*:\s*\{',prefix[end:]):return True
   if 'null' in prefix[end:]:break
 return False

class State:
 def __init__(self,admin,device,journal=None):
  self.admin=admin;self.device=device;self.pending=queue.Queue();self.requests={};self.results={};self.lock=threading.Lock();self.last_poll=0
  self.ready=threading.Condition(self.lock);self.active_session=None
  self.journal=journal;self.delivered=set();self.timings={}
  if journal is None:return
  journal.mkdir(parents=True,exist_ok=True,mode=0o700);journal.chmod(0o700)
  for path in journal.glob('*.json'):
   rid=path.stem
   if not re.fullmatch('[a-f0-9]{32}',rid):raise ValueError('Invalid journal entry')
   if journal_has_receipt(path):continue
   with path.open(encoding='utf-8') as f:entry=json.load(f)
   if entry.get('result') is not None:continue
   self.requests[rid]=entry['request']
   self.timings[rid]=entry.get('timings',{})
   if entry['delivered']:
    self.delivered.add(rid)
   else:
    # A restart never requeues input, even if delivery was not recorded.
    self.finish(rid,{'request_id':rid,'ok':False,'input_attempted':False,
                     'error':'BROKER_RESTART_BEFORE_DELIVERY_OBSERVE_AGAIN'})

 def persist(self,rid,request,delivered,result,timings):
  if self.journal is None:return
  path=self.journal/(rid+'.json');temp=self.journal/(rid+'.tmp')
  with os.fdopen(os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w',encoding='utf-8') as f:
   json.dump({'result':result,'request':request,'delivered':delivered,'timings':timings},f,ensure_ascii=False)
   f.flush();os.fsync(f.fileno())
  os.replace(temp,path)

 def archived_result(self,rid):
  if self.journal is None:return self.results.get(rid)
  try:
   with (self.journal/(rid+'.json')).open(encoding='utf-8') as f:return json.load(f)['result']
  except FileNotFoundError:raise ValueError('Unknown request') from None

 def submit(self,rid,req):
  if not re.fullmatch('[a-f0-9]{32}',rid):raise ValueError('Invalid request id')
  fields={k:v for k,v in req.items() if k not in ('action','expires')}
  req=make_request(req['action'],**fields)
  with self.lock:
   if rid in self.requests or (self.journal is not None and (self.journal/(rid+'.json')).exists()):
    raise ValueError('Duplicate id; recover instead')
   if any(i not in self.results and v['expires']>time.time() for i,v in self.requests.items()):raise ValueError('A request is still pending; recover first')
   timings={'submitted_unix':time.time()}
   self.persist(rid,req,False,None,timings)
   self.requests[rid]=req;self.timings[rid]=timings;self.pending.put(dict(id=rid,request=req))
   return {'request_id':rid}

 def finish(self,rid,value):
  # Called under the state lock (or during construction). Make the receipt
  # durable before waking waiters or dropping the outstanding request.
  self.persist(rid,self.requests[rid],rid in self.delivered,value,self.timings[rid])
  if self.journal is None:
   self.results[rid]=value
  else:
   del self.requests[rid],self.timings[rid]
   self.delivered.discard(rid)

 def result(self,rid,value):
  if not re.fullmatch('[a-f0-9]{32}',rid) or value.get('request_id')!=rid:raise ValueError('Unknown request')
  with self.lock:
   if rid in self.requests and rid not in self.results:
    self.timings[rid].setdefault('result_received_unix',time.time())
    self.finish(rid,value)
    self.ready.notify_all()
    return
  # Completed records are immutable. Reading a large duplicate cannot stall a
  # different request on the state lock; a conflicting resend stays rejected.
  prior=self.archived_result(rid)
  if prior is None:raise ValueError('Unknown request')
  if prior!=value:raise ValueError('Conflicting result')

 def wait_result(self,rid,timeout):
  if not re.fullmatch('[a-f0-9]{32}',rid) or not 0<=timeout<=20:raise ValueError('Invalid wait')
  with self.ready:
   if rid in self.requests:
    if not self.ready.wait_for(lambda:rid in self.results or rid not in self.requests,timeout):return None
   elif self.journal is None or not (self.journal/(rid+'.json')).is_file():
    raise ValueError('Unknown request')
  return self.archived_result(rid)

 def poll(self,session=None):
  if session is not None and not re.fullmatch('[a-f0-9]{32}',session):raise ValueError('Invalid session')
  with self.lock:
   if session is not None:self.active_session=session
   self.last_poll=time.time()
  try:item=self.pending.get(timeout=15)
  except queue.Empty:return None
  rid=item['id']
  with self.lock:
   if session is not None and session!=self.active_session:
    self.pending.put(item);return None
   if rid not in self.requests or rid in self.results:return None
   if item['request']['expires']<time.time():
    self.timings[rid]['result_received_unix']=time.time()
    self.finish(rid,{'ok':False,'request_id':rid,'error':'EXPIRED_BEFORE_DELIVERY','input_attempted':False})
    self.ready.notify_all();return None
   timings=dict(self.timings[rid],delivered_unix=time.time())
   self.persist(rid,self.requests[rid],True,None,timings)
   self.timings[rid]=timings;self.delivered.add(rid)
   # The device clock need not match the broker.
   item['valid_for_ms']=max(0,min(45000,int((item['request']['expires']-time.time())*1000)))
   return item

def handler(state):
 class Handler(http.server.BaseHTTPRequestHandler):
  protocol_version='HTTP/1.1'
  def setup(self):
   super().setup()
   self.connection.settimeout(30)
   self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
  def log_message(self,*a):pass
  def do_GET(self):self.dispatch()
  def do_POST(self):self.dispatch()
  def dispatch(self):
   token=self.headers.get('Authorization','').removeprefix('Bearer ')
   admin=hmac.compare_digest(token,state.admin);device=hmac.compare_digest(token,state.device)
   try:
    if not(admin or device):self.close_connection=True;self.reply(401,{'error':'Unauthorized'});return
    if self.command=='GET' and urlsplit(self.path).path=='/poll' and device:
     session=parse_qs(urlsplit(self.path).query).get('session',[None])[0]
     self.reply(200,state.poll(session));return
    if self.command=='GET' and self.path=='/health' and admin:self.reply(200,{'last_poll_age':time.time()-state.last_poll if state.last_poll else None});return
    if self.command=='GET' and self.path.startswith('/result/') and admin:
     parsed=urlsplit(self.path);rid=parsed.path.split('/')[-1]
     wait=float(parse_qs(parsed.query).get('wait',['0'])[0])
     value=state.wait_result(rid,wait)
     self.reply(200,value);return
    if self.command=='POST':
     n=int(self.headers.get('Content-Length','0'))
     if not 0<n<=24000000:raise ValueError('Invalid length')
     body=json.loads(self.rfile.read(n))
     if self.path=='/submit' and admin:self.reply(200,state.submit(body['id'],body['request']));return
     if self.path=='/result' and device:state.result(body['request_id'],body);self.reply(200,{'ok':True});return
    self.close_connection=True;self.reply(403,{'error':'Forbidden'})
   except (ValueError,KeyError,TypeError):self.close_connection=True;self.reply(400,{'error':'Invalid request'})
  def reply(self,status,value):
   raw=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)))
   if self.close_connection:self.send_header('Connection','close')
   self.end_headers()
   try:self.wfile.write(raw)
   except (BrokenPipeError,ConnectionResetError,ssl.SSLError):pass
 return Handler

def init_runtime(root,host,port):
 root.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(root,0o700)
 config=root/'config.json'
 if config.exists():raise ValueError('Runtime already exists; do not rotate active pairing')
 cnf=root/'openssl.cnf';cnf.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=Codex Desktop Relay\n[ext]\nsubjectAltName=IP:'+host+'\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n')
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','7','-keyout',str(root/'server.key'),'-out',str(root/'server.crt'),'-config',str(cnf)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 pem=(root/'server.crt').read_text();pin=hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
 cfg={'url':f'https://{host}:{port}','host':host,'port':port,'admin':secrets.token_hex(32),'device':secrets.token_hex(16),'pin':pin}
 config.write_text(json.dumps(cfg));config.chmod(0o600);(root/'server.key').chmod(0o600)
 print(json.dumps({'ready':True,'url':cfg['url'],'certificate_sha256':pin}))

def der(tag,body):
 n=len(body);length=bytes([n]) if n<128 else bytes([0x80+(n.bit_length()+7)//8])+n.to_bytes((n.bit_length()+7)//8,'big')
 return bytes([tag])+length+body

def pair(root,xml_path,output):
 import xml.etree.ElementTree as ET
 x=ET.fromstring(xml_path.read_text());nums=[]
 for name in ('Modulus','Exponent'):
  b=base64.b64decode(x.findtext(name));b=b.lstrip(b'\0') or b'\0';b=(b'\0'+b) if b[0]&128 else b;nums.append(der(2,b))
 pkcs1=der(0x30,b''.join(nums));alg=bytes.fromhex('300d06092a864886f70d0101010500');spki=der(0x30,alg+der(3,b'\0'+pkcs1))
 pub=root/'device-public.pem';b64=base64.b64encode(spki).decode();pub.write_text('-----BEGIN PUBLIC KEY-----\n'+'\n'.join(b64[i:i+64] for i in range(0,len(b64),64))+'\n-----END PUBLIC KEY-----\n')
 cfg=json.loads((root/'config.json').read_text());payload=json.dumps({'url':cfg['url'],'token':cfg['device'],'pin':cfg['pin']},separators=(',',':')).encode()
 # RSA OAEP SHA1 interoperates with Windows .NET Framework RSACryptoServiceProvider.
 encrypted=subprocess.check_output(['openssl','pkeyutl','-encrypt','-pubin','-inkey',str(pub),'-pkeyopt','rsa_padding_mode:oaep'],input=payload)
 output.write_bytes(encrypted);print(json.dumps({'encrypted_bytes':len(encrypted),'sha256':hashlib.sha256(encrypted).hexdigest()}))

class Client:
 """Single-threaded keep-alive client. Transport failure never retries a request."""
 def __init__(self,root):
  self.cfg=json.loads((root/'config.json').read_text())
  endpoint=urlsplit(self.cfg['url'])
  if endpoint.scheme!='https' or endpoint.username or endpoint.password:raise ValueError('HTTPS required')
  context=ssl.create_default_context(cafile=str(root/'server.crt'))
  context.minimum_version=ssl.TLSVersion.TLSv1_2
  self.connection=http.client.HTTPSConnection(endpoint.hostname,endpoint.port,context=context,timeout=25)
  self.last_completed=None
 def call(self,path,payload=None):
  try:
   # Server idle timeout is 30s. Retire an acknowledged idle connection BEFORE
   # sending a new request; this is not a retry of a failed or uncertain request.
   if self.last_completed is not None and time.monotonic()-self.last_completed>=20:
    self.connection.close()
   self.connection.request('POST' if payload is not None else 'GET',path,
    body=json.dumps(payload).encode() if payload is not None else None,
    headers={'Authorization':'Bearer '+self.cfg['admin'],'Content-Type':'application/json'})
   response=self.connection.getresponse();raw=response.read()
   self.last_completed=time.monotonic()
   if response.status!=200:raise RuntimeError('Relay HTTP '+str(response.status))
   return json.loads(raw)
  except Exception:
   self.connection.close()
   raise
 def close(self):self.connection.close()

_clients={}
def api(root,path,payload=None):
 # HTTPSConnection goes directly to the configured relay, ignoring proxy settings.
 key=str(root.resolve())
 if key not in _clients:_clients[key]=Client(root)
 return _clients[key].call(path,payload)

def main():
 p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);s=p.add_subparsers(dest='cmd',required=True)
 x=s.add_parser('init');x.add_argument('--host',required=True);x.add_argument('--port',type=int,default=18767)
 s.add_parser('serve');s.add_parser('health')
 x=s.add_parser('pair');x.add_argument('public_xml',type=Path);x.add_argument('output',type=Path)
 x=s.add_parser('request');x.add_argument('request_file',type=Path)
 x=s.add_parser('recover');x.add_argument('request_id')
 a=p.parse_args();r=a.runtime
 if a.cmd=='init':init_runtime(r,a.host,a.port);return
 if a.cmd=='pair':pair(r,a.public_xml,a.output);return
 if a.cmd=='serve':
  cfg=json.loads((r/'config.json').read_text())
  ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.minimum_version=ssl.TLSVersion.TLSv1_2
  ctx.load_cert_chain(str(r/'server.crt'),str(r/'server.key'))
  # Reserve the listener before recovery can modify an unfinished journal.
  with http.server.ThreadingHTTPServer((cfg['host'],cfg['port']),None) as server:
   server.socket=ctx.wrap_socket(server.socket,server_side=True);server.daemon_threads=True
   server.RequestHandlerClass=handler(State(cfg['admin'],cfg['device'],r/'journal'))
   print('TLS relay ready',flush=True);server.serve_forever()
  return
 if a.cmd=='health':print(json.dumps(api(r,'/health')));return
 rid=a.request_id if a.cmd=='recover' else uuid.uuid4().hex
 print(json.dumps({'request_id':rid,'note':'Never repeat uncertain input; recover this id.'}),flush=True)
 if a.cmd=='request':api(r,'/submit',{'id':rid,'request':json.loads(a.request_file.read_text())})
 deadline=time.time()+50
 while time.time()<deadline:
  response=api(r,'/result/'+rid+'?wait=20')
  if response is not None:
   value=save_result(response,r/'captures',rid,'windows-relay');print(json.dumps(value,ensure_ascii=False,indent=2));return
 raise RuntimeError('RESULT_UNCONFIRMED: recover by request ID; do not repeat input')
if __name__=='__main__':main()
