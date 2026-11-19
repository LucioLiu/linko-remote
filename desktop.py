#!/usr/bin/env python3
"""Mac controller. SSH transports a mailbox; interactive Windows worker owns UI.
Never automatically retry an action. No local window activation or input APIs.
"""
import argparse
import base64
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


def make_request(action, **kwargs):
    if action == 'job':
        from lanpilot.jobs import validate_job
        if set(kwargs) != {'manifest'}:
            raise ValueError('A local job requires exactly one manifest')
        validate_job(kwargs['manifest'])
        return dict(action=action, expires=int(time.time())+45, **kwargs)
    if 'image_format' in kwargs and kwargs['image_format'] not in ('png', 'compact'):
        raise ValueError('Unknown screenshot format')
    if action not in ('status', 'observe', 'click', 'doubleclick', 'text', 'key', 'scroll'):
        raise ValueError('Unsupported action')
    if action not in ('status', 'observe') and not re.fullmatch(r'[a-f0-9]{32}', kwargs.get('frame_id', '')):
        raise ValueError('Input requires the most recent frame_id for this host')
    if action in ('click', 'doubleclick'):
        if kwargs.get('button') not in ('left', 'right') or any(not isinstance(kwargs.get(k), int) or kwargs[k] < 0 for k in ('x', 'y')):
            raise ValueError('Click requires nonnegative pixel coordinates and a button')
        if action == 'doubleclick' and kwargs.get('button') != 'left':
            raise ValueError('Double-click supports the left button only')
    if action == 'text' and (not isinstance(kwargs.get('text'), str) or len(kwargs['text']) > 2000):
        raise ValueError('Text must be at most 2000 characters')
    if action == 'scroll' and (not isinstance(kwargs.get('amount'), int) or not 1 <= abs(kwargs['amount']) <= 10):
        raise ValueError('Scroll amount must be -10..-1 or 1..10')
    if action == 'key' and (not isinstance(kwargs.get('keys'), list) or not 1 <= len(kwargs['keys']) <= 4):
        raise ValueError('Supply one to four key names')
    if action == 'key':
        allowed={'CTRL','ALT','SHIFT','META','ENTER','TAB','ESC','BACKSPACE','DELETE','LEFT','UP','RIGHT','DOWN','HOME','END','PAGEUP','PAGEDOWN','SPACE','F4','F5'}
        if any(not isinstance(k,str) or (k not in allowed and not re.fullmatch('[A-Z0-9]',k)) for k in kwargs['keys']):
            raise ValueError('Unsupported key name')
    return dict(action=action, expires=int(time.time())+45, **kwargs)


def remote_script(request_id, request=None):
    if not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise ValueError('Invalid request ID')
    start = '''$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$root=Join-Path $env:LOCALAPPDATA 'CodexRemoteDesktop'
if(-not (Test-Path -LiteralPath $root)){throw 'WORKER_NOT_INSTALLED_OR_DIFFERENT_SSH_USER'}
'''
    start += "$id='"+request_id+"'\n"
    start += "$response=Join-Path $root ($id+'.response.json')\n"
    if request is not None:
        payload = base64.b64encode(json.dumps(request, ensure_ascii=False).encode()).decode()
        start += f"""$path=Join-Path $root ($id+'.request.json')
if((Test-Path -LiteralPath $path) -or (Test-Path -LiteralPath $response)){{throw 'DUPLICATE_ID'}}
[IO.File]::WriteAllBytes(($path+'.tmp'),[Convert]::FromBase64String('{payload}'))
Move-Item -LiteralPath ($path+'.tmp') -Destination $path
"""
    start += """$deadline=[DateTime]::UtcNow.AddSeconds(50)
while([DateTime]::UtcNow -lt $deadline){
 if(Test-Path -LiteralPath $response){
  [Console]::Write([IO.File]::ReadAllText($response)); exit 0
 }
 Start-Sleep -Milliseconds 200
}
throw 'RESULT_UNCONFIRMED_USE_RECOVER_DO_NOT_REPEAT_ACTION'
"""
    return start


def call(host, script, runner=subprocess.run):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,200}', host):
        raise ValueError('Use a trusted SSH config alias or user@host')
    # Keep payload off argv and below Windows command-line length limits.
    bootstrap = '$s=[Console]::In.ReadToEnd(); & ([scriptblock]::Create($s))'
    encoded = base64.b64encode(bootstrap.encode('utf-16-le')).decode()
    result = runner(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                     '-o', 'ConnectTimeout=8', '-o', 'ConnectionAttempts=1', host,
                     'powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand '+encoded],
                    input=script.encode('utf-8'), capture_output=True, timeout=65)
    if result.returncode:
        # Do not print full remote error: PowerShell can echo the command/payload.
        raise RuntimeError('SSH/worker request failed; result is unconfirmed. Check connection and recover by request ID.')
    return json.loads(result.stdout.decode('utf-8-sig'))


def jpeg_dimensions(raw):
    """Read a JPEG frame header without allocating a decoded image."""
    if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
        raise ValueError('Invalid JPEG screenshot')
    offset = 2
    dimensions = None
    while offset + 4 < len(raw) and raw[offset] == 0xff:
        marker = raw[offset + 1]
        if marker == 0xff:
            offset += 1
            continue
        size = int.from_bytes(raw[offset + 2:offset + 4], 'big')
        end = offset + 2 + size
        if size < 2 or end > len(raw) - 2:
            break
        if marker in (0xc0, 0xc1, 0xc2) and size >= 8:
            height = int.from_bytes(raw[offset + 5:offset + 7], 'big')
            width = int.from_bytes(raw[offset + 7:offset + 9], 'big')
            if width and height and width * height <= 50_000_000:
                dimensions = (width, height)
        if marker == 0xda and dimensions and end < len(raw) - 2:
            return dimensions
        offset = end
    raise ValueError('Invalid JPEG screenshot')


def save_result(response, directory, request_id, host):
    if response.get('request_id') != request_id:
        raise ValueError('Response ID mismatch')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    data = response.get('result', {})
    data.pop('screenshot', None)  # Only local files created below can be returned to MCP.
    image_fields = [field for field in ('png_base64', 'jpeg_base64') if field in data]
    if len(image_fields) > 1:
        raise ValueError('Ambiguous screenshot format')
    if image_fields:
        field = image_fields[0]
        raw = base64.b64decode(data[field], validate=True)
        if field == 'jpeg_base64':
            width, height = jpeg_dimensions(raw)
            bounds = data.get('bounds')
            if bounds is not None and list(bounds[2:]) != [width, height]:
                raise ValueError('JPEG dimensions do not match desktop bounds')
            suffix = '.jpg'
        else:
            if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('Invalid PNG screenshot')
            suffix = '.png'
        del data[field]
        target = directory / (request_id + suffix)
        target.write_bytes(raw)
        target.chmod(0o600)
        data['screenshot'] = str(target.resolve())
    artifact = data.get('artifact')
    if artifact is not None:
        if not isinstance(artifact, dict) or set(artifact) != {'name','sha256','bytes','content_base64'} or artifact['name'] != 'result.csv':
            raise ValueError('Invalid job artifact')
        if not isinstance(artifact['content_base64'], str) or len(artifact['content_base64']) > 2796204:
            raise ValueError('Job artifact exceeds size limit')
        raw = base64.b64decode(artifact['content_base64'], validate=True)
        if len(raw) > 2097152 or len(raw) != artifact['bytes'] or hashlib.sha256(raw).hexdigest() != artifact['sha256']:
            raise ValueError('Job artifact digest mismatch')
        raw.decode('utf-8')
        report = data.get('report')
        if report is not None and (report.get('csv_sha256') != artifact['sha256'] or report.get('csv_bytes') != len(raw)):
            raise ValueError('Job report differs from returned artifact')
        target = directory / (request_id + '.csv')
        target.write_bytes(raw)
        target.chmod(0o600)
        artifact.pop('content_base64')
        artifact['local_path'] = str(target.resolve())
    response['host'] = host
    target = directory / (request_id+'.json')
    target.write_text(json.dumps(response, ensure_ascii=False, indent=2))
    target.chmod(0o600)
    return response


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='Trusted SSH config alias; same Windows user as worker')
    parser.add_argument('--output', type=Path, default=Path.home()/'.codex-remote-desktop'/'captures')
    sub=parser.add_subparsers(dest='action', required=True)
    sub.add_parser('status');sub.add_parser('observe')
    recover=sub.add_parser('recover');recover.add_argument('request_id')
    for action in ('click','doubleclick','text','key','scroll'):
        p=sub.add_parser(action);p.add_argument('--frame', required=True)
        if action in ('click','doubleclick'):
            p.add_argument('x',type=int);p.add_argument('y',type=int);p.add_argument('--button',choices=['left','right'],default='left')
        elif action=='text': p.add_argument('--file',required=True,type=Path,help='UTF-8 text file; keeps typed text out of shell arguments')
        elif action=='key': p.add_argument('keys',nargs='+')
        elif action=='scroll':p.add_argument('amount',type=int)
    args=parser.parse_args()
    request_id=args.request_id if args.action=='recover' else uuid.uuid4().hex
    print(json.dumps({'request_id':request_id,'host':args.host,'note':'No automatic retry; recover this ID after an uncertain result.'}),file=sys.stderr)
    try:
        if args.action=='recover': script=remote_script(request_id)
        else:
            fields={}
            if hasattr(args,'frame'):fields['frame_id']=args.frame
            if args.action in ('click','doubleclick'):fields.update(x=args.x,y=args.y,button=args.button)
            if args.action=='text':fields['text']=args.file.read_text(encoding='utf-8')
            if args.action=='key':fields['keys']=[k.upper() for k in args.keys]
            if args.action=='scroll':fields['amount']=args.amount
            script=remote_script(request_id, make_request(args.action,**fields))
        result=save_result(call(args.host,script),args.output,request_id,args.host)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result.get('ok') else 2
    except Exception as exc:
        print(json.dumps({'ok':False,'request_id':request_id,'error':str(exc),'instruction':'Do not repeat input. Use recover, then observe.'}),file=sys.stderr)
        return 2

if __name__=='__main__':sys.exit(main())
