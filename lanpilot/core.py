"""Device registry and serialized, non-replaying requests. No desktop activation."""
import fcntl, json, os, re, time, uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from .transport import LocalClient as Client
from desktop import save_result, make_request

DEFAULT_HOME = Path.home() / 'Library/Application Support/LanPilot'
class Bridge:
 def __init__(self, home=DEFAULT_HOME):
  self.home=Path(home);self.home.mkdir(parents=True,exist_ok=True,mode=0o700)
  self.registry=self.home/'devices.json'
 def devices(self):
  if not self.registry.exists():return []
  return json.loads(self.registry.read_text())['devices']
 def device(self, name):
  matches=[d for d in self.devices() if name in [d['id'],d['name']]]
  if len(matches)!=1:raise ValueError('设备不存在或名称不唯一')
  return matches[0]
 def rename(self, identifier, name):
  name=name.strip()
  if not name or len(name)>100 or any(ord(c)<32 or ord(c)==127 for c in name):
   raise ValueError('请输入 1–100 个字符的电脑名称')
  with (self.home/'registry.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX)
   devices=self.devices()
   found=next((d for d in devices if d['id']==identifier),None)
   if found is None:raise ValueError('设备不存在')
   if any(d['id']!=identifier and name in (d['name'],d['id']) for d in devices):raise ValueError('设备名称已使用')
   found['name']=name
   from .wan_forward import atomic_write
   atomic_write(self.registry,json.dumps({'devices':devices},ensure_ascii=False).encode())
   return {'id':identifier,'name':name}
 def adopt(self, name, runtime):
  name=name.strip()
  if not name or len(name)>120:raise ValueError('请输入有效的设备名称')
  runtime=Path(runtime).resolve()
  cfg=json.loads((runtime/'config.json').read_text())
  for field in ['url','admin','device','pin']:
   if not cfg.get(field):raise ValueError('配对配置不完整')
  with (self.home/'registry.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX)
   devices=self.devices()
   if any(d['runtime']==str(runtime) for d in devices):raise ValueError('此设备已添加')
   for existing in devices:
    prior=json.loads((Path(existing['runtime'])/'config.json').read_text())
    if existing['name']==name:raise ValueError('设备名称已使用')
    if prior['device']==cfg['device']:raise ValueError('此配对身份已添加')
    if prior['port']==cfg['port']:raise ValueError('设备端口冲突，请分配独立端口')
   d={'id':uuid.uuid4().hex[:12],'name':name,'runtime':str(runtime)}
   devices.append(d);tmp=self.registry.with_suffix('.tmp')
   with os.fdopen(os.open(tmp,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600),'w') as f:json.dump({'devices':devices},f,ensure_ascii=False)
   os.replace(tmp,self.registry)
   return d
 @contextmanager
 def lock(self,d):
  # OS lock serializes separate MCP/CLI/UI processes as well as threads.
  p=self.home/(d['id']+'.lock')
  with open(p,'a') as f:
   os.chmod(p,0o600)
   fcntl.flock(f,fcntl.LOCK_EX)
   try:yield
   finally:fcntl.flock(f,fcntl.LOCK_UN)
 def health_all(self):
  devices=self.devices()
  if not devices:return []
  with ThreadPoolExecutor(max_workers=min(8,len(devices))) as pool:
   return list(pool.map(self._health,devices))
 def health(self,name):
  return self._health(self.device(name))
 def _health(self,d):
  client=None
  started=time.monotonic()
  try:
   client=Client(Path(d['runtime']))
   h=client.call('/health');age=h.get('last_poll_age')
   return {'id':d['id'],'name':d['name'],'online':age is not None and age<35,'heartbeat_age':age,'latency_ms':round((time.monotonic()-started)*1000),'state':'可连接' if age is not None and age<35 else '等待电脑上线'}
  except Exception:return {'id':d['id'],'name':d['name'],'online':False,'state':'连接中断','hint':'检查被控端邻控、远程地址和网络连接。'}
  finally:
   if client is not None:client.close()
 def request(self,name,request,request_id=None):
  if request is not None:
   request=dict(request)
   if request.get('action') in ('click','doubleclick'):request.setdefault('button','left')
   request=make_request(request['action'],**{k:v for k,v in request.items() if k not in ('action','expires')})
  d=self.device(name);root=Path(d['runtime'])
  rid=request_id or uuid.uuid4().hex
  if not re.fullmatch('[a-f0-9]{32}',rid):raise ValueError('Invalid request id')
  with self.lock(d):
   c=Client(root);started=time.monotonic()
   try:
    if request is not None:c.call('/submit',{'id':rid,'request':request})
    response=c.call('/result/'+rid+'?wait=20')
    if response is None:return {'ok':False,'state':'unconfirmed','request_id':rid,'instruction':'只查询此请求，不重发动作。'}
    response['elapsed_ms']=round((time.monotonic()-started)*1000)
    return save_result(response,root/'captures',rid,d['name'])
   except Exception:
    # Even a lost HTTP submit response may mean the action was queued.
    return {'ok':False,'state':'unconfirmed','request_id':rid,'instruction':'连接结果未确认；仅用 recover 查询原请求，不自动重发。'}
   finally:c.close()
