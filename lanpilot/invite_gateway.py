"""Pinned enrollment and multiplexed worker ingress; never expose admin routes."""
import argparse, hashlib, hmac, http.server, json, re, secrets, socket, ssl, subprocess, threading, time
from pathlib import Path
from urllib.parse import urlsplit
from .core import Bridge
from .invitations import claim, directory, profile
from .enrollment import local_address
from .wan_forward import WAN, atomic_write, definition, launch_definition, deployment_lock, load_definition
from .worker_gateway import WorkerGateway, WorkerHandler

class Handler(WorkerHandler):
    def reject(self,code):
        self.close_connection=True;self.send_error(code)
    def dispatch(self):
        path=urlsplit(self.path).path
        if not ((self.command=='POST' and self.path in ('/claim','/result')) or (self.command=='GET' and path=='/poll')):
            return self.reject(404)
        values=self.headers.get_all('Authorization',[])
        if len(values)!=1 or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{1,90}',values[0]):return self.reject(401)
        token=values[0][7:]
        if self.path=='/claim':
            lengths=self.headers.get_all('Content-Length',[])
            if len(lengths)!=1 or not re.fullmatch(r'[0-9]{1,4}',lengths[0]) or not 1<=int(lengths[0])<=6000 or self.headers.get('Transfer-Encoding'):return self.reject(400)
            try:
                req=json.loads(self.rfile.read(int(lengths[0])))
                if not isinstance(req,dict) or set(req)!={'public_key'}:raise ValueError()
                answer=claim(self.server.bridge,token,req['public_key']);data=json.dumps(answer).encode()
            except PermissionError:return self.reject(403)
            except Exception:return self.reject(400)
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        cfg=self.server.find(token)
        if not cfg:return self.reject(401)
        # WorkerHandler uses a request-local config here, never mutable cross-device state.
        self.worker_config=cfg
        return super().dispatch()

    do_POST=dispatch
    do_GET=dispatch

class Gateway(WorkerGateway):
    def __init__(self,bridge,host,port):
        self.bridge=bridge;self.context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);self.context.minimum_version=ssl.TLSVersion.TLSv1_2
        p=directory(bridge);self.context.load_cert_chain(p/'server.crt',p/'server.key')
        self.capacity=threading.BoundedSemaphore(80);self.guard=threading.Lock();self.previous=None;self.configs={}
        http.server.ThreadingHTTPServer.__init__(self,(host,port),Handler)
    def find(self,token):
        with self.guard:
            stamp=self.bridge.registry.stat().st_mtime_ns if self.bridge.registry.exists() else 0
            if stamp!=self.previous:
                configs={}
                for d in self.bridge.devices():
                    rt=Path(d['runtime'])
                    if rt.parent==(self.bridge.home/'devices').resolve() and re.fullmatch('invite-[a-f0-9]{32}',rt.name):
                        c=json.loads((rt/'config.json').read_text());configs[c['device']]=c
                self.configs=configs;self.previous=stamp
            return self.configs.get(token)

def ensure_endpoint(bridge,server=None,remote=None,lan_only=False):
    from .wan_forward import AGENTS
    if server is not None and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}',server):raise ValueError('invalid TCP relay host')
    if remote is not None and not 1<=remote<=65535:raise ValueError('invalid public port')
    p=directory(bridge)
    with deployment_lock():
        if (p/'endpoint.json').exists():
            cfg=profile(bridge)
            if (server and cfg.get('server')!=server) or (remote and cfg.get('remote')!=remote) or (lan_only and cfg['mode']!='lan'):raise ValueError('active invitation route cannot be silently changed')
        else:
            if not server and not lan_only:
                for f in sorted(AGENTS.glob('local.lanpilot.wan.tunnel.*.plist')):
                    prior=load_definition(f.stem)['ProgramArguments']
                    if '--to' in prior and '_forward' not in prior:
                        server=prior[prior.index('--to')+1];break
            if not server and not lan_only:raise ValueError('首次跨网接入请先配置中继：lanpilot invite-route --server 主机 --port 公网端口；仅局域网可用 --lan-only')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));local=sock.getsockname()[1]
            if server and not remote:remote=40000+secrets.randbelow(20000)
            if not (p/'server.key').exists() or not (p/'server.crt').exists():
                subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','3650','-subj','/CN=LanPilot Invitation Gateway','-keyout',str(p/'server.key'),'-out',str(p/'server.crt')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True);(p/'server.key').chmod(0o600)
            pin=hashlib.sha256(ssl.PEM_cert_to_DER_cert((p/'server.crt').read_text())).hexdigest()
            cfg={'mode':'wan' if server else 'lan','server':server,'remote':remote,'local':local,'pin':pin,
                 'url':f'https://{server}:{remote}' if server else f'https://{local_address()}:{local}'}
        gateway=definition('local.lanpilot.invitation.gateway',['/usr/bin/python3','-m','lanpilot.invite_gateway','--listen',str(cfg['local']),'--host','127.0.0.1' if cfg['mode']=='wan' else '0.0.0.0'])
        launch_definition(gateway)
        if cfg['mode']=='wan':
            binary=WAN/'bin/bore'
            if not binary.is_file():raise ValueError('请先安装 bore 到 '+str(binary))
            launch_definition(definition('local.lanpilot.invitation.tunnel',[str(binary),'local',str(cfg['local']),'--local-host','127.0.0.1','--to',cfg['server'],'--port',str(cfg['remote'])]))
        # Avoid issuing an unusable credential if the selected public port belongs to someone else.
        u=urlsplit(cfg['url']);ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);ctx.check_hostname=False;ctx.verify_mode=ssl.CERT_NONE
        for attempt in range(6):
            try:
                with socket.create_connection((u.hostname,u.port),2.5) as sock,ctx.wrap_socket(sock,server_hostname=u.hostname) as tls:
                    if not hmac.compare_digest(hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest(),cfg['pin']):raise ValueError('gateway identity mismatch')
                break
            except (OSError,ValueError):
                if attempt==5:raise ValueError('邀请网关未能通过真实路由的证书校验。请检查中继端口和连通性。')
                time.sleep(.4)
        atomic_write(p/'endpoint.json',json.dumps(cfg).encode());return cfg
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--listen',type=int,required=True);parser.add_argument('--host',required=True);args=parser.parse_args()
    with Gateway(Bridge(),args.host,args.listen) as gateway:gateway.serve_forever()
