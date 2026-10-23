"""Portable numeric exporter: explicit Codex session or normalized JSONL usage."""
import argparse,hashlib,json,os,sqlite3,subprocess,urllib.request,urllib.parse
from pathlib import Path

def normalize(u):
    i=u.get('input_tokens',u.get('prompt_tokens'));o=u.get('output_tokens',u.get('completion_tokens'))
    total=u.get('total_tokens',i+o if isinstance(i,int) and isinstance(o,int) else None)
    cached=(u.get('input_tokens_details',u.get('prompt_tokens_details',{})) or {}).get('cached_tokens',0)
    reason=(u.get('output_tokens_details',u.get('completion_tokens_details',{})) or {}).get('reasoning_tokens',0)
    if any(isinstance(x,bool) or not isinstance(x,int) or x<0 for x in (i,o,total,cached,reason)):raise ValueError('Invalid numeric usage')
    if total!=i+o or cached>i or reason>o:raise ValueError('Inconsistent numeric usage')
    return {'input_tokens':i,'output_tokens':o,'total_tokens':total,'input_tokens_details':{'cached_tokens':cached},'output_tokens_details':{'reasoning_tokens':reason}}

def codex(path,cfg,previous):
    meta={};total=last=None;model='unknown';models=set()
    with path.open(encoding='utf-8') as f:
        for line in f:
            try:r=json.loads(line)
            except ValueError:continue
            p=r.get('payload') or {}
            if r.get('type')=='session_meta':meta={k:p.get(k) for k in ('id','cwd')}
            if r.get('type')=='turn_context':
                model=p.get('model') or model;models.add(model)
            if r.get('type')=='event_msg' and p.get('type')=='token_count' and isinstance(p.get('info'),dict):
                info=p['info']
                if info.get('total_token_usage') and info.get('last_token_usage'):
                    def convert(u):return normalize({'input_tokens':u.get('input_tokens'),'output_tokens':u.get('output_tokens'),'total_tokens':u.get('total_tokens'),'input_tokens_details':{'cached_tokens':u.get('cached_input_tokens',0)},'output_tokens_details':{'reasoning_tokens':u.get('reasoning_output_tokens',0)}})
                    # Historical schemas/counters do not belong to the initial import.
                    # Select the latest complete event first, then validate that event.
                    total=info['total_token_usage'];last=info['last_token_usage']
    if meta.get('id')!=cfg['session_id'] or meta.get('cwd')!=cfg['cwd'] or total is None:raise ValueError('Session identity, workspace or counters unavailable')
    total=convert(total);last=convert(last)
    if previous==total:return [],total
    if previous:
        def flat(u):return [u['input_tokens'],u['output_tokens'],u['total_tokens'],u['input_tokens_details']['cached_tokens'],u['output_tokens_details']['reasoning_tokens']]
        d=[a-b for a,b in zip(flat(total),flat(previous))]
        last=normalize({'input_tokens':d[0],'output_tokens':d[1],'total_tokens':d[2],'input_tokens_details':{'cached_tokens':d[3]},'output_tokens_details':{'reasoning_tokens':d[4]}})
    eid='codex:'+hashlib.sha256((cfg['source_id']+meta['id']+json.dumps(total,sort_keys=True)).encode()).hexdigest()
    return [dict(event_id=eid,project=cfg['project'],model=model if len(models)<=1 else 'mixed-or-unknown',usage_json=json.dumps(last))],total

def events(path,cfg):
    out=[]
    with path.open(encoding='utf-8') as f:
        for line in f:
            try:r=json.loads(line)
            except ValueError:
                if not line.endswith('\n'):break
                raise ValueError('Malformed normalized export')
            if not isinstance(r.get('event_id'),str) or not r['event_id']:raise ValueError('Stable request event_id required')
            u=normalize(r['usage']);eid='export:'+hashlib.sha256((cfg['source_id']+':'+r['event_id']).encode()).hexdigest()
            out.append(dict(event_id=eid,project=cfg['project'],model=r.get('model','unknown'),usage_json=json.dumps(u)))
    return out

def anthropic_usage(u):
    # Anthropic input_tokens excludes cache reads AND cache creation.
    names=('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')
    values=[u.get(n,0) for n in names]
    if any(isinstance(v,bool) or not isinstance(v,int) or v<0 for v in values):
        raise ValueError('Invalid Anthropic usage')
    if 'input_tokens' not in u or 'output_tokens' not in u:raise ValueError('Incomplete Anthropic usage')
    i,o,read,create=values
    out=normalize({'input_tokens':i+read+create,'output_tokens':o,
                   'input_tokens_details':{'cached_tokens':read}})
    out['input_tokens_details']['cache_creation_tokens']=create
    return out

def claude(path,cfg):
    # Explicit approved transcript only. Local JSONL is version-dependent.
    selected={};matched=False
    with path.open(encoding='utf-8') as f:
        for line in f:
            try:r=json.loads(line)
            except ValueError:
                if not line.endswith('\n'):break
                raise ValueError('Malformed Claude transcript')
            if r.get('sessionId')!=cfg['session_id']:continue
            if r.get('cwd')!=cfg['cwd']:raise ValueError('Claude workspace mismatch')
            matched=True
            if r.get('type')!='assistant':continue
            m=r.get('message') or {};mid=m.get('id')
            if not isinstance(mid,str) or not mid or not isinstance(m.get('usage'),dict):continue
            # Never commit an unfinished streaming snapshot. Next run retries it.
            if not m.get('stop_reason'):continue
            usage=anthropic_usage(m['usage'])
            eid='claude:'+hashlib.sha256((cfg['source_id']+':'+cfg['session_id']+':'+mid).encode()).hexdigest()
            event=dict(event_id=eid,project=cfg['project'],model=m.get('model','unknown'),usage_json=json.dumps(usage))
            if mid in selected and selected[mid]!=event:raise ValueError('Changed final Claude message usage')
            selected[mid]=event
    if not matched:raise ValueError('Claude session identity unavailable')
    return list(selected.values())

def anthropic_events(path,cfg):
    # Numeric API/SDK export: final usage per stable request, no conversation text.
    out=[]
    with path.open(encoding='utf-8') as f:
        for line in f:
            try:r=json.loads(line)
            except ValueError:
                if not line.endswith('\n'):break
                raise ValueError('Malformed Anthropic export')
            if r.get('final') is not True:continue
            eid=r.get('event_id')
            if not isinstance(eid,str) or not eid:raise ValueError('Stable request event_id required')
            out.append(dict(event_id='anthropic:'+hashlib.sha256((cfg['source_id']+':'+eid).encode()).hexdigest(),
                project=cfg['project'],model=r.get('model','unknown'),usage_json=json.dumps(anthropic_usage(r['usage']))))
    return out

def rpc_decode(raw,kind):
    if 'text/event-stream' not in kind:return json.loads(raw)
    data=[];messages=[]
    for line in (raw+'\n\n').splitlines():
        if line.startswith('data:'):data.append(line[5:].lstrip())
        elif not line and data:
            messages.append(json.loads('\n'.join(data)));data=[]
    return next(x for x in messages if 'result' in x or 'error' in x)

def send(payload,transport):
    if transport['kind']=='command':
        # Agent supplies its own authenticated bridge. No shell evaluation or credentials in argv.
        r=subprocess.run(transport['argv'],input=json.dumps({'name':'gateway_usage_import','arguments':payload}),text=True,encoding='utf-8',capture_output=True,timeout=90,check=True)
        reply=json.loads(r.stdout)
    elif transport['kind']=='mcp_http':
        url=transport['url'];u=urllib.parse.urlparse(url)
        if u.scheme!='https' and not (u.scheme=='http' and u.hostname in ('localhost','127.0.0.1','::1')):raise ValueError('HTTPS required')
        token=os.environ.get(transport.get('token_env','GATEWAY_ACCESS_TOKEN'))
        if not token:raise ValueError('Gateway authentication unavailable')
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json, text/event-stream'}
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):raise ValueError('Redirects disabled for authenticated export')
        opener=urllib.request.build_opener(NoRedirect())
        def post(body):
            with opener.open(urllib.request.Request(url,data=json.dumps(body).encode(),headers=headers),timeout=45) as r:
                raw=r.read().decode();session=r.headers.get('Mcp-Session-Id')
                if session:headers['Mcp-Session-Id']=session
                return rpc_decode(raw,r.headers.get('Content-Type','')) if raw.strip() else {}
        init=post({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'portable-usage-export','version':'1.1.0'}}})
        headers['MCP-Protocol-Version']=init['result']['protocolVersion']
        post({'jsonrpc':'2.0','method':'notifications/initialized'})
        r=post({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'gateway_usage_import','arguments':payload}})
        if r.get('error') or r.get('result',{}).get('isError'):raise RuntimeError('Gateway rejected import')
        reply=json.loads(next(x['text'] for x in r['result']['content'] if x['type']=='text'))
    else:raise ValueError('Unsupported transport')
    if not reply.get('ok'):raise RuntimeError('Import not confirmed')
    return reply

def run(cfg,sender=send):
    state=Path(cfg['state']);state.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(state,timeout=2);db.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT)');db.commit()
    try:os.chmod(state,0o600)
    except OSError:pass # Windows ACLs must be configured by the installing agent.
    scope={k:cfg.get(k) for k in ('adapter','source_id','session_id','cwd','source','project')}
    key=hashlib.sha256(json.dumps(scope,sort_keys=True).encode()).hexdigest()
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT v FROM kv WHERE k=?',(key,)).fetchone();old=json.loads(row[0]) if row else {'seen':[],'baseline':None}
        if old.get('pending') is None:
            if cfg['adapter']=='codex_jsonl':batch,baseline=codex(Path(cfg['source']),cfg,old['baseline'])
            elif cfg['adapter']=='usage_jsonl':batch=events(Path(cfg['source']),cfg);baseline=None
            elif cfg['adapter']=='claude_code_jsonl':batch=claude(Path(cfg['source']),cfg);baseline=None
            elif cfg['adapter']=='anthropic_jsonl':batch=anthropic_events(Path(cfg['source']),cfg);baseline=None
            else:raise ValueError('No supported adapter; do not infer tokens from text')
            batch=[x for x in batch if x['event_id'] not in old['seen']]
            old['pending']={'batch':batch,'baseline':baseline}
            db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',(key,json.dumps(old)));db.commit();db.execute('BEGIN IMMEDIATE')
            old=json.loads(db.execute('SELECT v FROM kv WHERE k=?',(key,)).fetchone()[0])
            if old.get('pending') is None:
                db.commit();return {'ok':True,'events_confirmed':0,'quality':'client_reported'}
        # Persist pending range before sending. Ambiguous network failures replay exactly this range.
        for x in old['pending']['batch']:sender(x,cfg['transport'])
        n=len(old['pending']['batch']);old['seen']+= [x['event_id'] for x in old['pending']['batch']];old['baseline']=old['pending']['baseline'];old['pending']=None
        db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',(key,json.dumps(old)));db.commit()
        return {'ok':True,'events_confirmed':n,'quality':'client_reported'}
    except Exception:db.rollback();raise
    finally:db.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);a=p.parse_args()
    try:print(json.dumps(run(json.loads(Path(a.config).read_text(encoding='utf-8')))))
    except Exception as e:print(json.dumps({'ok':False,'error_type':type(e).__name__}));raise SystemExit(1)
