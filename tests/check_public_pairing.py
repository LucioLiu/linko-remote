"""Opt-in live protocol smoke test through a public TCP relay; no real PCs."""
import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import selectors
import ssl
import subprocess
import tempfile
import threading
import time
from contextlib import redirect_stdout
from lanpilot.core import Bridge
from lanpilot.invitations import directory
from lanpilot.invite_gateway import Gateway
from lanpilot.pairing_codes import issue_code
from lanpilot.broker import start_listener
from lanpilot.transport import LocalClient
from relay import init_runtime


def main():
 server='bore.pub';binary=Path.home()/'Library/Application Support/LanPilot/WAN/bin/bore'
 with tempfile.TemporaryDirectory(prefix='lanpilot-public-qa-') as tmp:
  root=Path(tmp);bridge=Bridge(root/'controller');p=directory(bridge)
  with redirect_stdout(io.StringIO()):init_runtime(p,'127.0.0.1',33333)
  cfg=json.loads((p/'config.json').read_text())
  gateway=Gateway(bridge,'127.0.0.1',0)
  thread=threading.Thread(target=gateway.serve_forever,daemon=True);thread.start()
  env=dict(os.environ,NO_COLOR='1')
  tunnel=subprocess.Popen([str(binary),'local',str(gateway.server_port),'--local-host','127.0.0.1','--to',server],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env)
  upstream=None;admin=None
  try:
   selector=selectors.DefaultSelector();selector.register(tunnel.stdout,selectors.EVENT_READ)
   data=b'';deadline=time.monotonic()+25;port=None
   while time.monotonic()<deadline:
    for key,_ in selector.select(1):
     chunk=os.read(key.fileobj.fileno(),4096)
     if not chunk:raise RuntimeError('public tunnel exited')
     data+=chunk
     match=re.search(rb'remote_port\s*[=:]\s*(\d+)',data)
     if match:port=int(match[1])
    if port:break
   selector.close()
   if not port:raise RuntimeError('public tunnel startup timed out')
   endpoint=f'https://{server}:{port}'
   (p/'endpoint.json').write_text(json.dumps({'url':endpoint,'pin':cfg['pin']}))
   def exchange(method,path,token,payload=None):
    cx=http.client.HTTPSConnection(server,port,context=ssl._create_unverified_context(),timeout=12)
    try:
     cx.connect()
     if hashlib.sha256(cx.sock.getpeercert(True)).hexdigest()!=cfg['pin']:raise RuntimeError('pin mismatch')
     cx.request(method,path,json.dumps(payload) if payload is not None else None,headers={'Authorization':'Bearer '+token})
     response=cx.getresponse();raw=response.read()
     if response.status!=200:raise RuntimeError('public request failed: '+str(response.status))
     return json.loads(raw)
    finally:cx.close()
   key=root/'rsa.pem';subprocess.run(['openssl','genrsa','-out',str(key),'2048'],capture_output=True,check=True)
   modulus=subprocess.check_output(['openssl','rsa','-in',str(key),'-noout','-modulus']).decode().strip().split('=')[1]
   xml='<RSAKeyValue><Modulus>'+base64.b64encode(bytes.fromhex(modulus)).decode()+'</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>'
   code=issue_code(bridge,'public-protocol-test')['code'].replace(' ','')
   reply=exchange('POST','/claim-code',code,{'public_key':xml})
   assert exchange('POST','/claim-code',code,{'public_key':xml})==reply
   assert len(bridge.devices())==1
   cipher=base64.b64decode(reply['pairing'])
   clear=subprocess.check_output(['openssl','pkeyutl','-decrypt','-inkey',str(key),'-pkeyopt','rsa_padding_mode:oaep'],input=cipher)
   identity=json.loads(clear);rt=Path(bridge.device(reply['device_id'])['runtime']);device_cfg=json.loads((rt/'config.json').read_text())
   upstream=start_listener(rt,device_cfg);admin=LocalClient(rt)
   rid='a'*32;admin.call('/submit',{'id':rid,'request':{'action':'observe'}})
   request=exchange('GET','/poll?session='+'b'*32,identity['token'])
   assert request['id']==rid
   exchange('POST','/result',identity['token'],{'request_id':rid,'ok':True,'result':{'protocol_test':True,'input_attempted':False}})
   assert admin.call('/result/'+rid+'?wait=0')['ok']
   print(json.dumps({'public_tls_pin':True,'code_exchange':True,'rsa_decrypt':True,'same_key_recovery':True,'worker_poll':True,'worker_receipt':True,'real_windows_desktop_test':False}))
  finally:
   if admin:admin.close()
   if upstream:upstream.shutdown();upstream.server_close()
   tunnel.terminate()
   try:tunnel.wait(timeout=5)
   except subprocess.TimeoutExpired:tunnel.kill();tunnel.wait()
   gateway.shutdown();gateway.server_close();thread.join()


if __name__=='__main__':main()
