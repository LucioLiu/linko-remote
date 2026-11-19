"""User-managed SSH shortcuts using system OpenSSH and its normal host checks."""
import fcntl
import json
import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path
from .wan_forward import atomic_write


def profiles(bridge):
    path=bridge.home/'ssh.json'
    return json.loads(path.read_text()) if path.exists() else []


def save(bridge,name,host,user='',port=22):
    name=name.strip();host=host.strip();user=user.strip()
    if not name or len(name)>100 or any(ord(c)<32 for c in name):raise ValueError('请输入有效的名称')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}',host):raise ValueError('请输入主机名、IP 或 SSH 配置别名')
    if user and not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}',user):raise ValueError('请输入有效的 SSH 用户名')
    if not 1<=port<=65535:raise ValueError('端口应为 1–65535')
    with (bridge.home/'ssh.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        items=profiles(bridge)
        if any(x['name']==name for x in items):raise ValueError('此 SSH 名称已使用')
        item={'id':uuid.uuid4().hex[:12],'name':name,'host':host,'user':user,'port':port}
        items.append(item);atomic_write(bridge.home/'ssh.json',json.dumps(items,ensure_ascii=False).encode())
    return item


def open_terminal(bridge,identifier):
    item=next((x for x in profiles(bridge) if x['id']==identifier),None)
    if not item:raise ValueError('SSH 连接不存在')
    # Revalidate stored data before creating an executable shortcut.
    host=item['host'];user=item['user'];port=int(item['port'])
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}',host) or (user and not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}',user)) or not 1<=port<=65535:raise ValueError('SSH 配置无效')
    folder=bridge.home/'ssh-shortcuts';folder.mkdir(mode=0o700,exist_ok=True)
    path=folder/(identifier+'.command')
    args=['/usr/bin/ssh','-p',str(port)] if port!=22 else ['/usr/bin/ssh']
    # Default port omission preserves custom Port in ~/.ssh/config aliases.
    if user:args+=['-l',user]
    args+=['--',host]
    atomic_write(path,('#!/bin/sh\nexec '+shlex.join(args)+'\n').encode());path.chmod(0o700)
    subprocess.run(['/usr/bin/open','-a','Terminal',str(path)],check=True,capture_output=True)
    return {'opened':True,'id':identifier}


def rename(bridge,identifier,name):
    name=name.strip()
    if not name or len(name)>100 or any(ord(c)<32 or ord(c)==127 for c in name):raise ValueError('请输入有效的名称')
    with (bridge.home/'ssh.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        items=profiles(bridge)
        target=next((x for x in items if x['id']==identifier),None)
        if not target:raise ValueError('SSH 连接不存在')
        if any(x['id']!=identifier and x['name']==name for x in items):raise ValueError('此 SSH 名称已使用')
        target['name']=name;atomic_write(bridge.home/'ssh.json',json.dumps(items,ensure_ascii=False).encode())
    return target
