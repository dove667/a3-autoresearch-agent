import json,os,time,urllib.request,urllib.error,traceback
from pathlib import Path
P=Path('/workspace'); api=os.environ['A3_GATEWAY_URL']; token=os.environ['A3_GATEWAY_TOKEN']; log=P/'trajectory.jsonl'
def req(path,method='GET',data=None):
 r=urllib.request.Request(api+path,None if data is None else json.dumps(data).encode(),{'Authorization':'Bearer '+token,'Content-Type':'application/json'},method=method)
 try:
  with urllib.request.urlopen(r,timeout=120) as f:return {'status':f.status,'body':json.loads(f.read())}
 except urllib.error.HTTPError as e:return {'status':e.code,'body':e.read().decode()}
def tool(a):
 op=a['op']; p=a.get('path','')
 if op in ('read','write'):
  x=(P/p).resolve() if not p.startswith('/') else Path(p).resolve()
  if not (x.is_relative_to(P) or op=='read' and (x.is_relative_to('/opt/a3') or x.is_relative_to('/run/input'))):raise ValueError('scope')
  if op=='write':x.parent.mkdir(parents=True,exist_ok=True);x.write_text(a['text']);return 'written'
  return sorted(q.name for q in x.iterdir()) if x.is_dir() else x.read_text()
 if op=='gateway':
  if not p.startswith(('/jobs','/sessions','/fixtures/isolation')):raise ValueError('scope')
  return req(p,a.get('method','GET'),a.get('data'))
 if op=='sleep':time.sleep(min(a.get('seconds',1),30));return 'ok'
 if op=='finish':return 'finished'
 raise ValueError('op')
schema={'type':'function','name':'research','description':'Fixed scope reads, workspace writes, gateway jobs/sessions/fixtures, sleep/finish. Artifacts are base64; large responses saved in raw. No secrets in files.','parameters':{'type':'object','properties':{'op':{'type':'string','enum':['read','write','gateway','sleep','finish']},'path':{'type':'string'},'text':{'type':'string'},'method':{'type':'string'},'data':{'type':'object'},'seconds':{'type':'number'}},'required':['op'],'additionalProperties':False}}
prompt='Matched harness workload: Read /opt/a3/docs/workflow_guide.md and /opt/a3/docs/contracts.md. Run a real remote A3 expert benchmark 2x5 basic source-column 1, three episodes seeds 16000-16002, 30 max steps, default success conditions; installed CLI python -m a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli; set MUJOCO_GL=egl by env command. Poll until complete and retrieve output artifact; report all per-episode cookies_in_target and success, protocol and errors in result.md. Observe results honestly; do not decode credentials. If errors resolve them. Finish once result recorded. No unrelated research. Token cost unknown.'
msgs=[{'role':'user','content':prompt}];n=0
while True:
 try:
  b=req('/v1/responses','POST',{'model':'openai/sandbox-model','input':msgs,'tools':[schema]})
  if b['status']!=200:raise RuntimeError(str(b))
  out=b['body'].get('output',[]);n+=1
  with log.open('a') as f:f.write(json.dumps({'event':'model','call':n,'output':out,'usage':b['body'].get('usage'),'cost':'unknown'})+'\n')
  msgs.extend(out);pending=[x for x in out if x.get('type')=='function_call'];done=False
  if not pending:msgs.append({'role':'user','content':'Continue until result.md is recorded and finish.'})
  for x in pending:
   a=json.loads(x['arguments'])
   try:res=tool(a);done |= a['op']=='finish'
   except Exception as e:res={'error':str(e),'traceback':traceback.format_exc()}
   text=json.dumps(res,ensure_ascii=False)
   with log.open('a') as f:f.write(json.dumps({'event':'tool','call':n,'arguments':a,'result':res},ensure_ascii=False)+'\n')
   if len(text)>12000:
    d=P/'raw';d.mkdir(exist_ok=True);q=d/(str(n)+'-'+x['call_id']+'.json');q.write_text(text);text=json.dumps({'saved':str(q),'preview':text[:10000],'length':len(text)})
   msgs.append({'type':'function_call_output','call_id':x['call_id'],'output':text})
  if done:break
 except Exception as e:
  with log.open('a') as f:f.write(json.dumps({'event':'error','error':str(e),'traceback':traceback.format_exc()})+'\n')
  time.sleep(10)
