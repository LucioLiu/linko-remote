"""Short-lived pairing codes for a reusable, controller-specific Windows kit.

The kit contains only the public gateway address and certificate pin. Codes
remain out of URLs, logs, archives and plaintext storage. Enrollment continues
to use the existing RSA/DPAPI identity and same-key recovery protocol.
"""
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import zipfile
from pathlib import Path

from .enrollment import INSTALL_FILES, WINDOWS
from .invitations import claim, directory, profile
from .wan_forward import atomic_write


def secret(bridge):
    path=directory(bridge)/'codes.secret'
    if not path.exists():
        # Callers hold enrollment.lock while first creating this controller key.
        atomic_write(path,secrets.token_bytes(32))
    return path.read_bytes()


def bearer(bridge, code):
    value=hmac.new(secret(bridge),code.encode(),hashlib.sha256).digest()
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def issue_code(bridge, name, minutes=10):
    name=name.strip()
    if not name or len(name)>100 or any(ord(c)<32 or ord(c)==127 for c in name):
        raise ValueError('请输入 1–100 个字符的电脑名称')
    if not 1<=minutes<=15:raise ValueError('验证码有效期为 1–15 分钟')
    profile(bridge)  # A verified route must already exist.
    p=directory(bridge)
    with (bridge.home/'enrollment.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if any(d['name']==name for d in bridge.devices()):raise ValueError('设备名称已使用')
        # Only one unclaimed short code is active; refresh revokes the old one.
        for path in p.glob('[a-f0-9]'*64+'.json'):
            state=json.loads(path.read_text())
            if state.get('short_code') and not state.get('response'):path.unlink()
        while True:
            code=''.join(str(secrets.randbelow(10)) for _ in range(12))
            token=bearer(bridge,code)
            record=p/(hashlib.sha256(token.encode()).hexdigest()+'.json')
            if not record.exists():break
        expires=int(time.time())+minutes*60
        atomic_write(record,json.dumps({'name':name,'expires':expires,'short_code':True}).encode())
    endpoint=profile(bridge)
    connection={'v':1,'url':endpoint['url'],'pin':endpoint['pin'],'code':code,'controller':'Linko Remote'}
    complete='linko:'+base64.urlsafe_b64encode(json.dumps(connection,separators=(',',':')).encode()).decode().rstrip('=')
    return {'code':' '.join(code[i:i+4] for i in range(0,12,4)),'connection':complete,
            'expires':expires,'name':name,'minutes':minutes}


class RateLimited(Exception):pass


def claim_code(bridge, code, xml, source, now=None):
    now=time.time() if now is None else now
    p=directory(bridge)
    with (bridge.home/'enrollment.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=p/'code-attempts.json'
        attempts=json.loads(path.read_text()) if path.exists() else []
        attempts=[a for a in attempts if now-60<a[0]<=now]
        address=hashlib.sha256(source.encode()).hexdigest()
        if len(attempts)>=60 or sum(a[1]==address for a in attempts)>=10:
            raise RateLimited('请稍候再试')
        attempts.append([now,address]);atomic_write(path,json.dumps(attempts).encode())
        if not re.fullmatch('[0-9]{12}',code):raise PermissionError('invalid pairing code')
        token=bearer(bridge,code)
        record=p/(hashlib.sha256(token.encode()).hexdigest()+'.json')
        if not record.exists() or not json.loads(record.read_text()).get('short_code'):
            raise PermissionError('invalid pairing code')
    return claim(bridge,token,xml,now)


def export_client(bridge, output, controller='我的 Mac', isolated_test=False, generic=False):
    """Reusable kit: intentionally contains no invitation, code or private key."""
    endpoint=profile(bridge) if not generic else {}
    if output.suffix.lower()!='.zip':raise ValueError('请保存为 .zip 安装包')
    created=False
    try:
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);created=True
        with os.fdopen(fd,'wb') as stream,zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
            sums={}
            for name in (*INSTALL_FILES,'Pair-Device.ps1','Claim-Invitation.ps1','Pair-Code.ps1'):
                data=(WINDOWS/name).read_bytes();archive.writestr(name,data)
                sums[name]=hashlib.sha256(data).hexdigest()
            meta={'controller':controller}
            if not generic:meta.update(url=endpoint['url'],pin=endpoint['pin'])
            if isolated_test:meta['isolated_test']=secrets.token_hex(16)
            archive.writestr('pairing-endpoint.json',json.dumps(meta,ensure_ascii=False))
            archive.writestr('manifest.json',json.dumps(sums))
            archive.writestr('打开邻控.cmd','@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Pair-Code.ps1"\r\n')
    except BaseException:
        if created:output.unlink(missing_ok=True)
        raise
    return {'installer':str(output),'reusable':True,'controller':controller}
