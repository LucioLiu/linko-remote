"""Time only the approved fixture's installed CLI operation; never replay an unconfirmed job."""
import argparse,base64,hashlib,json,subprocess,time
from pathlib import Path
from decode_receipt import decode

CLI=Path.home()/'Applications/邻控.app/Contents/Resources/lanpilot'

def cli(argv):
    proc=subprocess.run([str(CLI),*argv],check=True,capture_output=True,text=True,timeout=75)
    return json.loads(proc.stdout)

def request(log,device,argv,name):
    t=time.time();res=cli(argv);row=dict(requested=t*1000,action=name,rid=res.get('request_id'),recoveries=0,finished=time.time()*1000);log['records'].append(row)
    while res.get('state')=='unconfirmed' and row['recoveries']<3:
        row['recoveries']+=1;res=cli(['recover',device,row['rid']]);row['finished']=time.time()*1000
    if not res.get('ok'):raise RuntimeError((name,row['rid'],res.get('error'),res.get('state')))
    return res

def main():
    p=argparse.ArgumentParser();p.add_argument('device');p.add_argument('workspace',type=Path);p.add_argument('--stage',action='store_true');p.add_argument('--retry-final-screen',action='store_true');a=p.parse_args();d=a.device;w=a.workspace
    tag='staging' if a.stage else 'B'
    mainfile=w/f'{d}-{tag}.json'
    if mainfile.exists() and not a.retry_final_screen:raise RuntimeError('Previous audit exists; do not re-dispatch.')
    if a.retry_final_screen:log=json.loads(mainfile.read_text())
    else:log={'start':time.time()*1000,'records':[]}
    save=lambda:mainfile.write_text(json.dumps(log,ensure_ascii=False,indent=2))
    try:
        if not a.retry_final_screen or not log.get('artifact'):
            manifest=w/('stage-cold.json' if a.stage else 'reconcile-foreground.json')
            if a.retry_final_screen:
                r=cli(['recover',d,log['records'][0]['rid']])
                if not r.get('ok'):raise RuntimeError('Original receipt not confirmed')
            else:
                log['start']=time.time()*1000
                r=request(log,d,['job',d,str(manifest)],'stage-inputs' if a.stage else 'reconcile-job')
            log.setdefault('job_verified_ms',time.time()*1000);out=r['result']
            if a.stage:
                if out['operation']!='stage-inputs' or len(out['files'])!=12:raise ValueError('Did not verify all staged inputs')
                log['staged_bytes']=sum(x['bytes'] for x in out['files']);log['finish']=time.time()*1000;return
            artifact=out['artifact'];data=Path(artifact['local_path']).read_bytes() if 'local_path' in artifact else base64.b64decode(artifact['content_base64'],validate=True)
            if len(data)!=artifact['bytes'] or hashlib.sha256(data).hexdigest()!=artifact['sha256']:raise ValueError('Invalid direct CLI artifact')
            f=w/f'{d}-B.csv';f.write_bytes(data);log['artifact']=str(f);log['worker_ms']=out['elapsed_ms'];log['report']=out['report']
            subprocess.run(['python3',str(w/'check.py'),str(f)],check=True,capture_output=True)
            log['result_verified_ms']=time.time()*1000;save()
        r=request(log,d,['observe',d],'verify-final-screen')
        log['foreground_metadata']=r['result']['title']  # Read-only proof checks actual synthetic frame bytes.
        actual=decode(Path(r['result']['screenshot']));expected=Path(log['artifact']).read_bytes()
        if actual!=expected:raise ValueError('GUI evidence disagrees with CLI artifact')
        log['screenshot']=r['result']['screenshot'];log['finish']=time.time()*1000
    finally:
        save()
        if 'finish' in log:print(json.dumps(dict(device=d,mode=tag,verified_wall_seconds=round((log['finish']-log['start'])/1000,3),dispatches=len(log['records']),recoveries=sum(r['recoveries'] for r in log['records']))))
if __name__=='__main__':main()
