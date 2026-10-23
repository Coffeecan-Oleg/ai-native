"""Gateway metering: provider usage and MCP result volume are separate metrics."""
import asyncio,hashlib,json,logging,math,os,re,sqlite3,time,uuid
from contextlib import contextmanager
from pathlib import Path
LOG=logging.getLogger(__name__)
VERSION='1.0.0'
SAFE_READ={'gateway_knowledge_document_get','gateway_knowledge_search','gateway_memory_search','gateway_knowledge_space_get','gateway_company_context','gateway_file_read'}
def db_path():
    p=Path(os.getenv('GATEWAY_KNOWLEDGE_VAULT_PATH','/data/company-knowledge-vault'))/'.usage-meter'
    p.mkdir(parents=True,exist_ok=True,mode=0o700)
    return p/'events.sqlite'
@contextmanager
def connect():
    p=db_path();c=sqlite3.connect(p,timeout=5);c.row_factory=sqlite3.Row
    try:
        c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA busy_timeout=5000')
        c.execute('''CREATE TABLE IF NOT EXISTS events(actor TEXT NOT NULL, event_id TEXT NOT NULL, at REAL NOT NULL, project TEXT NOT NULL, metric TEXT NOT NULL, quality TEXT NOT NULL, source TEXT NOT NULL, model TEXT NOT NULL, tool TEXT NOT NULL, in_count INTEGER, out_count INTEGER, total_count INTEGER, cached INTEGER, reasoning INTEGER, characters INTEGER, bytes INTEGER, cost REAL, is_test INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(actor,event_id,metric))''')
        c.execute('CREATE INDEX IF NOT EXISTS event_scope ON events(actor,at,project)');os.chmod(p,0o600)
        with c:
            yield c
    finally:
        c.close()

def label(v):
    v=str(v or '').strip()
    if len(v)>128 or not re.fullmatch(r'[\w:./@+-]*',v):return ''
    return v

def count(v):
    if isinstance(v,bool) or not isinstance(v,int) or v<0:raise ValueError('Usage counts must be nonnegative integers')
    return v

def normalize_usage(u):
    if not isinstance(u,dict):raise ValueError('usage must be an object')
    i=u.get('input_tokens',u.get('prompt_tokens'));o=u.get('output_tokens',u.get('completion_tokens'))
    if i is None or o is None:raise ValueError('Provider usage lacks input/output counts')
    i=count(i);o=count(o);total=count(u.get('total_tokens',i+o))
    if total!=i+o:raise ValueError('Usage total differs from input plus output')
    di=u.get('input_tokens_details',u.get('prompt_tokens_details',{})) or {};do=u.get('output_tokens_details',u.get('completion_tokens_details',{})) or {}
    cached=count(di.get('cached_tokens',0));reasoning=count(do.get('reasoning_tokens',0))
    if cached>i or reasoning>o:raise ValueError('Usage detail exceeds parent count')
    return {'in_count':i,'out_count':o,'total_count':total,'cached':cached,'reasoning':reasoning}

def insert(actor,event_id,project,metric,quality,source,model='',tool='',is_test=False,**values):
    if not actor.subject or actor.subject=='anonymous':return False
    row={'actor':actor.subject,'event_id':label(event_id) or uuid.uuid4().hex,'at':time.time(),'project':label(project) or 'unassigned','metric':metric,'quality':quality,'source':source,'model':label(model),'tool':label(tool),'is_test':int(is_test),**values}
    allowed={'in_count','out_count','total_count','cached','reasoning','characters','bytes','cost'}
    if set(values)-allowed:raise ValueError('Unexpected observation field')
    with connect() as c:
        cols=list(row);cur=c.execute('INSERT OR IGNORE INTO events('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+')',list(row.values()))
        return cur.rowcount==1

def provider_usage(actor,event_id,body,project='',is_test=False):
    if not isinstance(body,dict) or not isinstance(body.get('usage'),dict):
        insert(actor,event_id,project,'coverage','unavailable','llm_proxy',is_test=is_test);return False
    usage=normalize_usage(body['usage'])
    added=insert(actor,event_id,project,'model_usage','provider_reported','llm_proxy',model=body.get('model',''),is_test=is_test,**usage)
    if added and not is_test:
        # Compatibility with the repository's existing telemetry tools. Never mirror context volume as model tokens.
        try:
            from gateway_mcp.services.telemetry import record_usage_report
            record_usage_report(actor=actor,agent='gateway-model-proxy',source='gateway-meter',source_quality='actual',event_name='provider_usage',correlation_id=event_id,project=label(project),model=label(body.get('model','')),input_tokens=usage['in_count'],output_tokens=usage['out_count'],total_tokens=usage['total_count'],cache_read_input_tokens=usage['cached'],reasoning_tokens=usage['reasoning'])
        except Exception:LOG.warning('usage compatibility mirror unavailable')
    return added

def observed_project(arguments,actor):
    # This pilot has no implicit external source catalogue or local-account lookup.
    if arguments.get('scope') == 'user': return 'personal'
    space = str(arguments.get('space', ''))
    if space.startswith('personal'): return 'personal'
    if space in ('space:organizational-awareness', 'group:organizational-awareness'):
        return 'platforma-training'
    if str(arguments.get('tool_name', '')).startswith('platforma.'):
        return 'platforma-training'
    return 'unassigned'

def context_observation(actor,name,arguments,result,event_id):
    if name not in SAFE_READ:
        if name!='gateway_call_tool':return False
        route=str(arguments.get('tool_name',''))
        if route not in {'platforma.context.get','platforma.services.list','platforma.services.get','platforma.requests.list','platforma.requests.get'}:return False
    root=getattr(result,'root',result)
    if getattr(root,'isError',False):return False
    texts=[x.text for x in getattr(root,'content',[]) if getattr(x,'type','')=='text']
    if not texts:return False
    for t in texts:
        try:
            payload=json.loads(t)
            if isinstance(payload,dict) and (payload.get('ok') is False or payload.get('content_base64')):return False
        except ValueError:pass
    chars=sum(map(len,texts));size=sum(len(t.encode('utf-8')) for t in texts)
    return insert(actor,event_id,observed_project(arguments,actor),'context_volume','measured_result_size','mcp',tool=name,characters=chars,bytes=size)

def dashboard(actor,days=30,project=''):
    where='actor=? AND at>=? AND is_test=0';params=[actor.subject,time.time()-max(1,min(int(days),365))*86400]
    if project:where+=' AND project=?';params.append(label(project))
    with connect() as c:
        rows=[dict(r) for r in c.execute('SELECT project,metric,quality,model,tool,count(*) AS events,sum(in_count) AS input_tokens,sum(out_count) AS output_tokens,sum(total_count) AS total_tokens,sum(cached) AS cached_tokens,sum(reasoning) AS reasoning_tokens,sum(characters) AS returned_characters,sum(bytes) AS returned_bytes,sum(cost) AS cost_usd FROM events WHERE '+where+' GROUP BY project,metric,quality,model,tool ORDER BY project,metric',params)]
    return {'ok':True,'version':VERSION,'scope':'current_actor','days':days,'rows':rows,'coverage':{'exact':'LLM requests through Gateway proxy with provider usage only','context':'Selected successful MCP read results; does not prove model consumed the complete result','client_chats':'not_automatically_visible','cost':'unknown unless supplied from billing; no model prices assumed'},'context_is_not_billed_tokens':True}

def register(server):
    from gateway_mcp.services.auth import current_actor,require_scope
    from mcp.types import CallToolRequest,ToolAnnotations
    @server.tool(annotations=ToolAnnotations(title='Gateway Usage Dashboard',readOnlyHint=True))
    async def gateway_usage_dashboard(days:int=30,project:str='')->str:
        actor=require_scope('telemetry:read',tool='gateway_usage_dashboard')
        return json.dumps(await asyncio.to_thread(dashboard,actor,days,project),ensure_ascii=False)
    @server.tool(annotations=ToolAnnotations(title='Gateway Import Client Usage'))
    async def gateway_usage_import(event_id:str,project:str,model:str,usage_json:str)->str:
        actor=require_scope('telemetry:write',tool='gateway_usage_import')
        usage=normalize_usage(json.loads(usage_json))
        if not label(event_id):raise ValueError('Stable export event ID required')
        added=await asyncio.to_thread(insert,actor,event_id,project,'model_usage','client_reported','client_export',model=model,**usage)
        return json.dumps({'ok':True,'inserted':added,'quality':'client_reported','provider_verified':False})
    handler=server._mcp_server.request_handlers[CallToolRequest]
    async def metered(req):
        result=await handler(req)
        try:
            await asyncio.to_thread(context_observation,current_actor(),req.params.name,req.params.arguments or {},result,uuid.uuid4().hex)
        except Exception:LOG.warning('context measurement unavailable')
        return result
    server._mcp_server.request_handlers[CallToolRequest]=metered
