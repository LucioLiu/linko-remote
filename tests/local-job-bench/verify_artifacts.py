import argparse,csv,hashlib,json
from pathlib import Path
def read(path):
 with path.open(encoding='utf-8-sig',newline='') as stream:
  reader=csv.DictReader(stream)
  fields=['batch','id','expected_cents','actual_cents','status']
  if reader.fieldnames!=fields:raise ValueError(f'Unexpected columns: {reader.fieldnames}')
  return [(int(r['batch']),r['id'],*(int(r[f]) if r[f]!='' else None for f in fields[2:4]),r['status']) for r in reader]
p=argparse.ArgumentParser();p.add_argument('artifacts',nargs='+',type=Path)
args=p.parse_args(); oracle=read(Path('oracle.csv'))
for f in args.artifacts:
 rows=read(f)
 if rows!=oracle:raise ValueError(f'{f}: differs from independent row-by-row oracle')
 print(json.dumps(dict(file=f.name,rows=len(rows),sha256=hashlib.sha256(f.read_bytes()).hexdigest(),delta_cents=sum((y or 0)-(x or 0) for _,_,x,y,_ in rows))))
