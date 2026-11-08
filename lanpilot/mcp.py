"""JSON-RPC MCP over stdio; stdout contains protocol only."""
import sys,json,base64
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from .core import Bridge
from . import VERSION
INSTRUCTIONS='邻控：后台控制已配对的另一台Windows电脑，不打开或抢占本机窗口。先list_devices，再observe看图；输入必须携带该观察的frame_id，检查回执和画面后再下一步。连接失败不改用UU或本机前台；未确认结果只recover，不重发。业务提交权限仍取决于用户任务。'
TOOLS=[
 {'name':'list_devices','description':'列出邻控已配对电脑与实时在线状态；不发送键鼠。','inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
 {'name':'observe','description':'后台获取另一台电脑截图与一次性frame_id；不会切换Mac前台。','inputSchema':{'type':'object','properties':{'device':{'type':'string'}},'required':['device'],'additionalProperties':False}},
 {'name':'act','description':'在已观察的远端画面上执行一次动作。禁止自动重试；先确认用户任务授权。','inputSchema':{'type':'object','properties':{'device':{'type':'string'},'request':{'type':'object','properties':{'action':{'enum':['click','doubleclick','text','key','scroll']},'frame_id':{'type':'string','pattern':'^[a-f0-9]{32}$'},'x':{'type':'integer','minimum':0},'y':{'type':'integer','minimum':0},'button':{'enum':['left','right'],'default':'left'},'text':{'type':'string','maxLength':2000},'keys':{'type':'array','items':{'type':'string'}},'amount':{'type':'integer'}},'required':['action','frame_id'],'additionalProperties':False}},'required':['device','request'],'additionalProperties':False}},
 {'name':'recover','description':'只读查询原request_id的结果，不重新执行。','inputSchema':{'type':'object','properties':{'device':{'type':'string'},'request_id':{'type':'string'}},'required':['device','request_id'],'additionalProperties':False}}
]
TOOLS.extend([
 {'name':'stage_inputs','description':'将指定 Mac UTF-8 CSV 校验后传到 Windows 专用输入目录。同名目录只有文件逐字一致时才可复用。',
  'inputSchema':{'type':'object','properties':{'device':{'type':'string'},'input_scope':{'type':'string'},
    'files':{'type':'array','minItems':1,'maxItems':48,'items':{'anyOf':[{'type':'string','description':'Mac 本地 CSV 路径'},{'type':'object','properties':{'path':{'type':'string'},'name':{'type':'string'}},'required':['path','name'],'additionalProperties':False}]}}},
    'required':['device','input_scope','files'],'additionalProperties':False}},
 {'name':'run_job','description':'在 Windows 的独立目录执行有范围限制的 reconcile-ledgers 或 read-artifact 作业；用 recover 查询原请求，不重跑。返回 CSV 的本机路径和校验值。',
  'inputSchema':{'type':'object','properties':{'device':{'type':'string'},'manifest':{'type':'object',
    'properties':{'version':{'enum':[1]},'operation':{'enum':['reconcile-ledgers','read-artifact','job-status','job-result']},'input_scope':{'type':'string'},
      'execution':{'enum':['foreground','background']},'job_id':{'type':'string','pattern':'^[a-f0-9]{32}$'},'receipt_scope':{'type':'string'},'batches':{'type':'array','maxItems':24,'items':{'type':'object','properties':{'batch':{'type':'string'},'expected_sha256':{'type':'string'},'actual_sha256':{'type':'string'}},'required':['batch','expected_sha256','actual_sha256'],'additionalProperties':False}},
      'file':{'enum':['result.csv']},'sha256':{'type':'string'}},'required':['version','operation'],'additionalProperties':False}},
    'required':['device','manifest'],'additionalProperties':False}}
])
image_format={'enum':['png','compact'],'description':'远程网络可选全尺寸压缩图，至少节省 25% 才使用。默认原始 PNG。'}
for tool in TOOLS:
 if tool['name']=='observe':tool['inputSchema']['properties']['image_format']=image_format
 if tool['name']=='act':tool['inputSchema']['properties']['request']['properties']['image_format']=image_format
for t in TOOLS:t['annotations']={'readOnlyHint':t['name'] in ('list_devices','observe','recover'),'destructiveHint':t['name']=='act','idempotentHint':t['name'] in ('list_devices','observe','recover','stage_inputs'),'openWorldHint':False}
def invoke(b,name,args):
 if name=='list_devices':r=b.health_all()
 elif name=='observe':
  request={'action':'observe'}
  if 'image_format' in args:request['image_format']=args['image_format']
  r=b.request(args['device'],request)
 elif name=='act':
  req=args['request']
  if req.get('action') not in ['click','doubleclick','text','key','scroll']:raise ValueError('不支持的动作')
  r=b.request(args['device'],req)
 elif name=='stage_inputs':
  from .jobs import stage_manifest
  r=b.request(args['device'],{'action':'job','manifest':stage_manifest(args['input_scope'],args['files'])})
 elif name=='run_job':
  manifest=args['manifest']
  if manifest.get('operation') not in ('reconcile-ledgers','read-artifact','job-status','job-result'):raise ValueError('不支持的作业')
  r=b.request(args['device'],{'action':'job','manifest':manifest})
 elif name=='recover':r=b.request(args['device'],None,args['request_id'])
 else:raise ValueError('未知工具')
 content=[{'type':'text','text':json.dumps(r,ensure_ascii=False)}]
 if isinstance(r,dict):
  path=r.get('result',{}).get('screenshot')
  if path:
   with open(path,'rb') as f:content.append({'type':'image','mimeType':'image/jpeg' if path.endswith('.jpg') else 'image/png','data':base64.b64encode(f.read()).decode()})
 return {'content':content,'isError':isinstance(r,dict) and r.get('ok') is False}
def main():
 b=Bridge()
 # One FIFO per paired machine preserves observation/action order while another
 # machine may be waiting for its screen. Only the stdout writer is shared.
 machines={}
 listings=ThreadPoolExecutor(max_workers=1)
 output_lock=Lock()

 def reply(request_id, **fields):
  message=json.dumps(dict(jsonrpc="2.0",id=request_id,**fields),ensure_ascii=False)
  with output_lock:
   print(message,flush=True)

 def fail(request_id):
  reply(request_id,error={'code':-32602,'message':'邻控请求失败：检查设备与参数；不重发未确认输入。'})

 def call(request_id,params):
  try:
   reply(request_id,result=invoke(b,params['name'],params.get('arguments',{})))
  except Exception:
   fail(request_id)

 try:
  for line in sys.stdin:
   request_id=None
   try:
    m=json.loads(line)
    if 'id' not in m:continue
    request_id=m['id']
    method=m.get('method')
    if method=='tools/call':
     params=m['params']
     if params['name'] in ('observe','act','recover','stage_inputs','run_job'):
      device=b.device(params['arguments']['device'])['id']
      if device not in machines:
       machines[device]=ThreadPoolExecutor(max_workers=1)
      machines[device].submit(call,request_id,params)
     else:
      listings.submit(call,request_id,params)
     continue
    if method=='initialize':result={'protocolVersion':m['params'].get('protocolVersion','2024-11-05'),'capabilities':{'tools':{}},'serverInfo':{'name':'lanpilot','version':VERSION},'instructions':INSTRUCTIONS}
    elif method=='ping':result={}
    elif method=='tools/list':result={'tools':TOOLS}
    else:
     reply(request_id,error={'code':-32601,'message':'Method not found'})
     continue
    reply(request_id,result=result)
   except Exception:
    fail(request_id)
 finally:
  for executor in machines.values():
   executor.shutdown(wait=True)
  listings.shutdown(wait=True)

if __name__=='__main__':main()
