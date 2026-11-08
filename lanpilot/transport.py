"""Local admin connects via loopback; peer identity is the stored TLS SHA256 pin."""
import ssl,http.client,json,hashlib,hmac
from relay import Client
class PinnedConnection(http.client.HTTPSConnection):
 def __init__(self,port,pin):
  ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);ctx.check_hostname=False;ctx.verify_mode=ssl.CERT_NONE;ctx.minimum_version=ssl.TLSVersion.TLSv1_2
  super().__init__('127.0.0.1',port,context=ctx,timeout=25);self.pin=pin
 def connect(self):
  super().connect()
  actual=hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
  if not hmac.compare_digest(actual,self.pin):self.close();raise ssl.SSLError('TLS_IDENTITY_MISMATCH')
class LocalClient(Client):
 def __init__(self,root):
  self.cfg=json.loads((root/'config.json').read_text())
  self.connection=PinnedConnection(self.cfg['port'],self.cfg['pin']);self.last_completed=None
