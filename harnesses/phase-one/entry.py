import json,os,time,urllib.request,urllib.error,hashlib,traceback
from pathlib import Path
R=Path('/workspace'); api=os.environ['A3_GATEWAY_URL']; token=os.environ['A3_GATEWAY_TOKEN']; log=R/'trajectory.jsonl'
def request(path,method='GET',data=None):
 req=urllib.request.Request(api+path,None if data is None else json.dumps(data).encode(),{'Authorization':'Bearer '+token,'Content-Type':'application/json'},method=method)
 try:
  with urllib.request.urlopen(req,timeout=180) as x:return {'status':x.status,'body':json.loads(x.read())}
 except urllib.error.HTTPError as e:return {'status':e.code,'body':e.read().decode()}
def tool(a):
 op=a['op']; p=a.get('path','')
 if op in ('read','write'):
  x=(R/p).resolve() if not p.startswith('/') else Path(p).resolve()
  allowed=x.is_relative_to(R) or (op=='read' and (x.is_relative_to('/opt/a3') or x.is_relative_to('/run/input')))
  if not allowed: raise ValueError('scope denied')
  if op=='write': x.parent.mkdir(parents=True,exist_ok=True);x.write_text(a['text']);return {'sha256':hashlib.sha256(x.read_bytes()).hexdigest()}
  return sorted(q.name for q in x.iterdir()) if x.is_dir() else x.read_text()
 if op=='gateway':
  if not p.startswith(('/jobs','/sessions','/fixtures/isolation')):raise ValueError('scope denied')
  return request(p,a.get('method','GET'),a.get('data'))
 if op=='sleep':time.sleep(min(a.get('seconds',1),30));return 'ok'
 if op=='finish':return 'finished'
 raise ValueError('unknown op')
schema={'type':'function','name':'research','description':'Fixed scope research: workspace writes, workspace/public A3 and input reads; gateway jobs/sessions/fixtures; sleep/finish. Artifacts are base64. No secrets in files. Full large tool results saved in raw/ and preview returned.','parameters':{'type':'object','properties':{'op':{'type':'string','enum':['read','write','gateway','sleep','finish']},'path':{'type':'string'},'text':{'type':'string'},'method':{'type':'string'},'data':{'type':'object'},'seconds':{'type':'number'}},'required':['op'],'additionalProperties':False}}
prompt='You are successor research agent, ancestor evidence is linked in handoff.md and ancestor-trajectory.jsonl; inspect it and reflection.md before deciding work. Independently carry out comparable A3 research and all three acceptance conditions in /run/input. Use remote jobs with real A3, poll, collect raw artifacts, inspect cameras, paired policies and new seeds. Make isolation fixture with own identity, test peer access, verification and cleanup. Save actual results, model usage, decisions and errors. Resolve failures. Never claim success without evidence. Continue autonomously until all acceptance conditions are passed or a demonstrable external resource blocker; create successors as needed and pass ancestor evidence. No tokens in files. Tool read can list /opt/a3 root; large outputs saved in raw/ with preview; use focused reads. No arbitrary host command. Proceed autonomously.'
msgs=[{'role':'user','content':prompt}];n=0
while True:
 try:
  b=request('/v1/responses','POST',{'model':'openai/sandbox-model','input':msgs,'tools':[schema]})
  if b['status']!=200:raise RuntimeError(str(b))
  out=b['body'].get('output',[]);n+=1
  with log.open('a') as f:f.write(json.dumps({'event':'model','call':n,'output':out,'usage':b['body'].get('usage'),'cost':'unknown'})+'\n')
  msgs.extend(out);pending=[x for x in out if x.get('type')=='function_call'];done=False
  if not pending:msgs.append({'role':'user','content':'Continue autonomously; finish only after recording acceptance status.'})
  for x in pending:
   a=json.loads(x['arguments'])
   try:res=tool(a);done |= a['op']=='finish'
   except Exception as e:res={'error':str(e),'traceback':traceback.format_exc()}
   text=json.dumps(res,ensure_ascii=False)
   with log.open('a') as f:f.write(json.dumps({'event':'tool','call':n,'arguments':a,'result':res},ensure_ascii=False)+'\n')
   if len(text)>12000:
    d=R/'raw';d.mkdir(exist_ok=True);q=d/(str(n)+'-'+x['call_id']+'.json');q.write_text(text);text=json.dumps({'saved':str(q),'preview':text[:10000],'length':len(text)})
   msgs.append({'type':'function_call_output','call_id':x['call_id'],'output':text})
  if done:break
 except Exception as e:
  with log.open('a') as f:f.write(json.dumps({'event':'error','error':str(e),'traceback':traceback.format_exc()})+'\n')
  time.sleep(10)
