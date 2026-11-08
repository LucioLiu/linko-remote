"""Single-use invitations. The bearer never appears in a URL or a log."""
import base64, contextlib, fcntl, hashlib, io, json, os, re, secrets, shutil, time, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from .enrollment import WINDOWS, INSTALL_FILES, available_port, local_address
from .wan_forward import atomic_write
from relay import init_runtime, pair

def directory(bridge):
    p=bridge.home/'invitations';p.mkdir(mode=0o700,exist_ok=True);return p

def profile(bridge):
    p=directory(bridge)
    return json.loads((p/'endpoint.json').read_text())

def public_key(xml):
    if not isinstance(xml,str) or len(xml)>4096 or '<!' in xml:raise ValueError('invalid RSA public key')
    e=ET.fromstring(xml)
    if e.tag!='RSAKeyValue' or sorted(n.tag for n in e)!=['Exponent','Modulus'] or any(n.attrib or list(n) for n in e):raise ValueError('invalid RSA public key')
    modulus=base64.b64decode(e.findtext('Modulus'),validate=True);exp=base64.b64decode(e.findtext('Exponent'),validate=True)
    if len(modulus)!=256 or not modulus[0]&128 or not modulus[-1]&1 or exp!=b'\x01\x00\x01':raise ValueError('invalid RSA-2048 public key')
    return '<RSAKeyValue><Modulus>'+base64.b64encode(modulus).decode()+'</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>'

def claim(bridge,token,xml,now=None):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{43}',token):raise PermissionError('invalid invitation')
    xml=public_key(xml)
    p=directory(bridge);fp=hashlib.sha256(xml.encode()).hexdigest();identifier=hashlib.sha256(token.encode()).hexdigest(); record=p/(identifier+'.json')
    with (bridge.home/'enrollment.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if not record.is_file():raise PermissionError('invalid invitation')
        state=json.loads(record.read_text())
        if (now if now is not None else time.time())>=state['expires'] or state.get('key',fp)!=fp:raise PermissionError('invalid invitation')
        known=state.get('response')
        if known:return known # recover same encrypted response; no other key may replay
        state['key']=fp;atomic_write(record,json.dumps(state).encode())
        rt=(bridge.home/'devices'/('invite-'+identifier[:32])).resolve();devices=bridge.devices()
        found=next((d for d in devices if Path(d['runtime'])==rt),None)
        if not found:
            if any(d['name']==state['name'] for d in devices):raise ValueError('device name already registered')
            devices_dir=rt.parent;devices_dir.mkdir(mode=0o700,exist_ok=True)
            # If the controller failed before publishing a prior attempt, discard that unadopted directory.
            if rt.exists():shutil.rmtree(rt)
            try:
                port=available_port({json.loads((Path(d['runtime'])/'config.json').read_text())['port'] for d in devices})
                with contextlib.redirect_stdout(io.StringIO()):init_runtime(rt,local_address(),port)
                cfg=json.loads((rt/'config.json').read_text()); endpoint=profile(bridge)
                for name in ('server.crt','server.key'): shutil.copyfile(p/name,rt/name)
                cfg['pin']=endpoint['pin'];atomic_write(rt/'config.json',json.dumps(cfg).encode())
                (rt/'key.xml').write_text(xml)
                with contextlib.redirect_stdout(io.StringIO()):pair(rt,rt/'key.xml',rt/'pairing.bin')
                for d in devices:
                    prior=Path(d['runtime'])/'device-public.pem'
                    if prior.is_file() and prior.read_bytes()==(rt/'device-public.pem').read_bytes():raise ValueError('already paired Windows key')
                found=bridge.adopt(state['name'],rt)
            except BaseException:
                if not any(Path(d['runtime'])==rt for d in bridge.devices()):shutil.rmtree(rt,ignore_errors=True)
                raise
        reply={'device_id':found['id'],'pairing':base64.b64encode((rt/'pairing.bin').read_bytes()).decode(),'route':profile(bridge)['url']}
        state['response']=reply;atomic_write(record,json.dumps(state).encode());return reply

def issue(bridge,name,output,minutes=15,isolated_test=False):
    name=name.strip()
    if not name or len(name)>100 or any(ord(c)<32 for c in name) or not 1<=minutes<=60:raise ValueError('invalid device name or lifetime')
    if output.suffix.lower()!='.cmd':raise ValueError('邀请安装文件必须以 .cmd 结尾')
    endpoint=profile(bridge);p=directory(bridge);token=secrets.token_urlsafe(32);expires=int(time.time())+60*minutes
    record=p/(hashlib.sha256(token.encode()).hexdigest()+'.json')
    embedded={'version':1,'name':name,'url':endpoint['url'],'pin':endpoint['pin'],'token':token,'expires':expires}
    if isolated_test:embedded['isolated_test']=secrets.token_hex(16)
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as z:
        sums={}
        for n in (*INSTALL_FILES,'Pair-Device.ps1','Claim-Invitation.ps1'):
            b=(WINDOWS/n).read_bytes();z.writestr(n,b);sums[n]=hashlib.sha256(b).hexdigest()
        z.writestr('manifest.json',json.dumps(sums));z.writestr('invitation.json',json.dumps(embedded))
    # No untrusted path interpolated in PS. One CMD shell opens an unprivileged user installer.
    preamble='''@echo off
setlocal DisableDelayedExpansion
set "LP_INVITE_FILE=%~f0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop';$d=Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString('N'));[IO.Directory]::CreateDirectory($d)|Out-Null;try{$t=[IO.File]::ReadAllLines($env:LP_INVITE_FILE);$i=[Array]::IndexOf($t,'::PAYLOAD');if($i -lt 0){throw 'Missing payload'};$p=Join-Path $d 'kit.zip';[IO.File]::WriteAllBytes($p,[Convert]::FromBase64String(($t[($i+1)..($t.Length-1)] -join '')));Add-Type -AssemblyName System.IO.Compression.FileSystem;[IO.Compression.ZipFile]::ExtractToDirectory($p,$d); & (Join-Path $d 'Claim-Invitation.ps1')}finally{Remove-Item -LiteralPath $d -Recurse -Force}"
set "LP_RC=%errorlevel%"
if not "%LP_RC%"=="0" pause
exit /b %LP_RC%
::PAYLOAD
'''
    raw=preamble.replace('\n','\r\n').encode()+base64.encodebytes(data.getvalue()).replace(b'\n',b'\r\n')
    with (bridge.home/'enrollment.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if any(d['name']==name for d in bridge.devices()):raise ValueError('设备名称已使用')
        for f in p.glob('*.json'):
            if f.name=='endpoint.json':continue
            s=json.loads(f.read_text())
            if s['expires']<=time.time():f.unlink()
            elif s['name']==name:raise ValueError('该设备已有未过期邀请')
        atomic_write(record,json.dumps({'name':name,'expires':expires}).encode())
        created=False
        try:
            fd=os.open(output,os.O_CREAT|os.O_WRONLY|os.O_EXCL,0o600);created=True
            with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
        except BaseException:
            record.unlink()
            if created:output.unlink(missing_ok=True)
            raise
    return {'installer':str(output),'expires':expires,'minutes':minutes,'route':endpoint['url']}
