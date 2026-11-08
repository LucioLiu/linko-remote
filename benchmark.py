#!/usr/bin/env python3
"""Read-only, bounded relay measurement. Never sends or retries desktop input."""
import argparse,json,os,statistics,time,uuid
from pathlib import Path
from relay import api

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--runtime',required=True,type=Path)
 p.add_argument('--samples',type=int,default=10)
 p.add_argument('--interval',type=float,default=0)
 p.add_argument('--action',choices=('status','observe'),default='status')
 p.add_argument('--output',type=Path,required=True)
 a=p.parse_args()
 if not 1<=a.samples<=1000 or not 0<=a.interval<=60:p.error('samples 1..1000; interval 0..60 seconds')
 a.output.parent.mkdir(parents=True,exist_ok=True)
 # Never persist screenshots or window titles in benchmark logs.
 rows=[]
 with os.fdopen(os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as output:
  for i in range(a.samples):
   rid=uuid.uuid4().hex;start=time.monotonic()
   row={'request_id':rid,'action':a.action,'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
   try:
    api(a.runtime,'/submit',{'id':rid,'request':{'action':a.action}})
    result=api(a.runtime,'/result/'+rid+'?wait=20')
    if result is None:raise RuntimeError('RESULT_UNCONFIRMED; recover '+rid+' before new requests')
    row.update(seconds=round(time.monotonic()-start,4),ok=result['ok'],
               session=result.get('result',{}).get('session'),error=result.get('error'))
    row.update(worker_ms=result.get('worker_ms'),
               previous_post_ms=result.get('result',{}).get('previous_post_ms'),
               previous_cleanup_ms=result.get('result',{}).get('previous_cleanup_ms'))
   except Exception as error:
    row.update(seconds=round(time.monotonic()-start,4),ok=False,error=type(error).__name__,
               recovery='Recover this request ID; this tool stops without retry')
   output.write(json.dumps(row)+'\n');output.flush();rows.append(row)
   if not row['ok']:break
   if i+1<a.samples and a.interval:time.sleep(a.interval)
 values=[r['seconds'] for r in rows if r['ok']]
 print(json.dumps({'action':a.action,'completed':len(rows),'requested':a.samples,
                   'success':len(values),'median_seconds':statistics.median(values) if values else None,
                   'max_seconds':max(values) if values else None,
                   'sessions':sorted(set(r['session'] for r in rows if r.get('session'))),
                   'output':str(a.output.resolve())}))
 if len(values)!=a.samples:raise SystemExit(2)

if __name__=='__main__':main()
