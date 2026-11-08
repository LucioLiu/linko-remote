import csv, io, json, zipfile
from pathlib import Path
from collections import Counter
from decimal import Decimal
from random import Random
rng=Random(1109)
def csvdata(rows):
 f=io.StringIO(newline='')
 csv.writer(f).writerows(rows)
 return f.getvalue().encode('utf-8')
def money(c):
 return str(Decimal(c).scaleb(-2))
oracle=[]; count=0
with zipfile.ZipFile('inputs.zip','w',zipfile.ZIP_DEFLATED) as z:
 for i in range(1,7):
  sides=[[],[]]
  for n in range(1,121):
   key=f'R{n:04}'
   value=rng.randrange(-8000,750000)
   for s in range(2):
    if (s==0 and n%43==0) or (s==1 and n%29==0):continue
    total=value+(175 if s==1 and n%17==0 else 0)
    parts=[total-201,201] if n%11==0 else [total]
    sides[s].extend((key,money(c)) for c in parts)
  totals=[]
  for tag,rows in zip(['expected','actual'],sides):
   rng.shuffle(rows)
   z.writestr(f'{tag}-{i:02}.csv',csvdata([['id','amount'],*rows]))
   count+=len(rows)
   counter=Counter()
   for k,v in rows:counter[k]+=int(Decimal(v)*100)
   totals.append(counter)
  a,b=totals
  for k in sorted(a.keys()|b.keys()):
   x,y=a.get(k),b.get(k)
   if x!=y:oracle.append([f'{i:02}',k,x,y,'missing_expected' if x is None else 'missing_actual' if y is None else 'different'])
Path('oracle.csv').write_bytes(csvdata([['batch','id','expected_cents','actual_cents','status'],*oracle]))
print(count,len(oracle),sum((y or 0)-(x or 0) for _,_,x,y,_ in oracle))
