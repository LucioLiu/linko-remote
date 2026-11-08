"""Authenticated LAN discovery. It advertises addresses, never grants trust."""
import hashlib,hmac,json,secrets,socket,time
PORT=18770

def device_id(token):return hashlib.sha256(token.encode()).hexdigest()[:16]
def sign(token,text):return hmac.new(token.encode(),text.encode(),hashlib.sha256).hexdigest()
def request(token,nonce=None):
 nonce=nonce or secrets.token_hex(16)
 return {'v':1,'device':device_id(token),'nonce':nonce,'mac':sign(token,'discover:'+nonce)}
def response(cfg,req,address):
 nonce=req.get('nonce','')
 if len(nonce)!=32 or not all(c in '0123456789abcdef' for c in nonce):return None
 if req.get('device')!=device_id(cfg['device']) or not hmac.compare_digest(req.get('mac',''),sign(cfg['device'],'discover:'+nonce)):return None
 url='https://'+address+':'+str(cfg['port'])
 return {'v':1,'nonce':nonce,'url':url,'mac':sign(cfg['device'],'endpoint:'+nonce+':'+url)}
def verify(token,nonce,r):
 return r.get('nonce')==nonce and hmac.compare_digest(r.get('mac',''),sign(token,'endpoint:'+nonce+':'+r.get('url','')))
def serve(configs,stop):
 sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('',PORT));sock.settimeout(1)
 try:
  while not stop.is_set():
   try:
    data,peer=sock.recvfrom(4096);req=json.loads(data)
    if not isinstance(req,dict):continue
    # Route lookup chooses the actual local interface on the peer's subnet.
    route=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try:route.connect(peer);address=route.getsockname()[0]
    finally:route.close()
    for cfg in configs():
     r=response(cfg,req,address)
     if r:
      sock.sendto(json.dumps(r).encode(),peer)
      print(json.dumps({'event':'discovery_reply','at':int(time.time()),'peer':peer[0],'endpoint':r['url']}),flush=True)
      break
   except (socket.timeout,ValueError,KeyError,TypeError,OSError):continue
 finally:sock.close()
