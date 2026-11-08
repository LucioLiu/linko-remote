"""Summarize a synthetic A/B run. These are logical payload lengths, not TCP usage."""
import argparse, base64, csv, hashlib, json
from pathlib import Path


def compact_bytes(v):
    return len(json.dumps(v,ensure_ascii=False,separators=(',',':')).encode())


def summarize(home, device, run):
    dev=next(x for x in json.loads((home/'devices.json').read_text())['devices'] if x['id']==device)
    journal=Path(dev['runtime'])/'journal'
    summary=dict(device=device,wall_s=(run['finish']-run['start'])/1000,
                 dispatches=0,recovery_queries=0,unconfirmed=0,rejected_before_input=0,
                 request_json_bytes=0,result_json_bytes=0,screen_image_bytes=0,artifact_bytes_returned_inline=0,
                 rpc_wait_s=0.0, records=[])
    for rec in run['records']:
        x=json.loads((journal/(rec['rid']+'.json')).read_text());res=x.get('result')
        req=x['request'];rq=compact_bytes(req);rs=compact_bytes(res) if res else 0
        data=(res or {}).get('result',{});image_bs=sum(len(base64.b64decode(data[k])) for k in ('png_base64','jpeg_base64') if k in data)
        artifact=data.get('artifact',{});ab=len(base64.b64decode(artifact['content_base64'])) if 'content_base64' in artifact else 0
        summary['dispatches']+=1;summary['recovery_queries']+=rec.get('recoveries',0);summary['unconfirmed']+=res is None
        summary['rejected_before_input']+=bool(res and not res['ok'] and res.get('input_attempted') is False)
        summary['request_json_bytes']+=rq;summary['result_json_bytes']+=rs;summary['screen_image_bytes']+=image_bs;summary['artifact_bytes_returned_inline']+=ab
        wait=(rec.get('finished',rec['requested'])-rec['requested'])/1000;summary['rpc_wait_s']+=wait
        summary['records'].append(dict(rid=rec['rid'],action=req['action'],verified=bool(res and res.get('ok')),wait_s=round(wait,3),request_json_bytes=rq,result_json_bytes=rs,screen_bytes=image_bs,artifact_bytes=ab,timings=x.get('timings',{})))
    summary['rpc_wait_s']=round(summary['rpc_wait_s'],3)
    summary['overhead_note']='Canonical UTF-8 compact JSON only, measured across recorded request and result objects. Image bytes included in base64 JSON (do not add again). Omits TLS/TCP, polling, failed/repeated transmissions, and broker envelope.'
    if 'artifact' in run:
        p=Path(run['artifact']);b=p.read_bytes();summary['artifact_bytes']=len(b);summary['artifact_sha256']=hashlib.sha256(b).hexdigest()
    return summary

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('device');p.add_argument('run',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
    s=summarize(Path.home()/'Library/Application Support/LanPilot',a.device,json.loads(a.run.read_text()));a.output.write_text(json.dumps(s,ensure_ascii=False,indent=2));print({k:v for k,v in s.items() if k not in ('records','overhead_note')})
