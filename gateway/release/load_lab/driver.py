"""Bounded MCP TCP client and fake HTTP provider; never uses real credentials."""
import asyncio,json,math,os,threading,time
from collections import Counter
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
import httpx

class Provider:
    def __init__(self):self.lock=threading.Lock();self.gate=threading.Event();self.mode='ok';self.delay=.5;self.active=0;self.peak=0;self.calls=0
    def reset(self,mode='ok',delay=.5):
        with self.lock:
            assert self.active==0,'provider_still_active';self.mode=mode;self.delay=delay;self.peak=0;self.calls=0;self.gate.clear()
    def stats(self):
        with self.lock:return {'calls':self.calls,'peak_active':self.peak,'active':self.active}
P=Provider()
class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def do_POST(self):
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<4096:self.send_error(400);return
        body=parse_qs(self.rfile.read(size).decode());method=self.path.rsplit('/',1)[-1]
        if body.get('api_key')!=['synthetic-load-key-not-real'] or method not in {'getLists','getContactCount'}:self.send_error(403);return
        with P.lock:P.calls+=1;P.active+=1;P.peak=max(P.peak,P.active);mode,delay=P.mode,P.delay
        try:
            if mode=='probe':assert P.gate.wait(10),'probe_gate_deadline'
            payload={'result':[{'id':12345,'title':'Synthetic load list'}]} if method=='getLists' else {'result':{'listId':12345,'count':12,'searchParams':{'type':'address'}}}
            data=json.dumps(payload).encode()
            if mode=='trickle':
                self.send_response(200);self.send_header('Content-Length',str(len(data)+20));self.end_headers()
                for _ in range(20):self.wfile.write(b' ');self.wfile.flush();time.sleep(2)
                self.wfile.write(data);self.wfile.flush()
            else:
                time.sleep(delay);self.send_response(503 if mode=='failure' else 429 if mode=='429' else 200)
                if mode=='429':self.send_header('Retry-After','2')
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass
        finally:
            with P.lock:P.active-=1
    def log_message(self,*args):pass

def percentile(values,q):return round(sorted(values)[max(0,math.ceil(len(values)*q)-1)],2)

async def run():
    assert os.getenv('PLATFORMA_LOAD_LAB')=='disposable-v1'
    server=ThreadingHTTPServer(('0.0.0.0',9000),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    clients=[];seq=100;results=[];budget_failures=[];started=time.monotonic()
    control=httpx.AsyncClient(base_url='http://load-gateway:8000',timeout=45,trust_env=False)
    async def command(action):
        r=await control.post('/__load__/control',headers={'x-load-control':os.environ['LOAD_CONTROL']},json={'action':action});r.raise_for_status();return r.json()
    def unpack(r):
        if not r.content:return {}
        if 'text/event-stream' in r.headers.get('content-type',''):
            return next(json.loads(line[5:].strip()) for line in r.text.splitlines() if line.startswith('data:') and any(k in line for k in ['"result"','"error"']))
        return r.json()
    async def rpc(client,body):
        r=await client.post('/mcp',json={'jsonrpc':'2.0',**body});assert r.status_code in {200,202},('mcp_http',r.status_code)
        if r.headers.get('mcp-session-id'):client.headers['Mcp-Session-Id']=r.headers['mcp-session-id']
        return unpack(r)
    async def call(index,method='unisender.getLists'):
        nonlocal seq
        seq+=1;rid=seq;t=time.monotonic()
        args={'tool_name':method}
        if method=='unisender.getContactCount':args['arguments_json']='{"list_id":12345,"contact_type":"address"}'
        obj=await rpc(clients[index],{'id':rid,'method':'tools/call','params':{'name':'gateway_call_tool','arguments':args}})
        result=obj.get('result',{});text=next((x['text'] for x in result.get('content',[]) if x['type']=='text'),'')
        if result.get('isError') or 'error' in obj:return {'status':'mcp_error','ms':(time.monotonic()-t)*1000}
        value=json.loads(text);return {'status':int(value.get('status',0)),'code':value.get('error_code'),
            'contacted':value.get('upstream_contacted',value.get('data',{}).get('upstream_contacted')),
            'request_id_present':bool(value.get('gateway_request_id')),'ms':(time.monotonic()-t)*1000}
    async def record(name,values,elapsed):
        state=await command('state');stats=P.stats();assert state['slots']==0,('leaked_slots',name)
        row={'name':name,'requests':len(values),'statuses':dict(Counter(str(v['status']) for v in values)),
             'codes':dict(Counter(v.get('code') or 'ok' for v in values)),
             'p50_ms':percentile([v['ms'] for v in values],.5),'p95_ms':percentile([v['ms'] for v in values],.95),
             'success_p95_ms':percentile([v['ms'] for v in values if v['status']==200],.95) if any(v['status']==200 for v in values) else None,
             'denial_p95_ms':percentile([v['ms'] for v in values if v['status']!=200],.95) if any(v['status']!=200 for v in values) else None,
             'max_ms':round(max(v['ms'] for v in values),2),'elapsed_seconds':round(elapsed,3),
             'provider':stats,'slots_after':state['slots'],'diagnostics':state.get('diagnostics',{})}
        results.append(row);Path('/results/progress.json').write_text(json.dumps({'phase':name,'cases':results},indent=2));return row
    async def reset(mode='ok',delay=.5):
        assert time.monotonic()-started<200,'load_deadline'
        for _ in range(10):
            if P.stats()['active']==0:break
            await asyncio.sleep(.1)
        await command('reset');P.reset(mode,delay)
    try:
        for _ in range(120):
            try:
                health=await control.get('/healthz')
                if health.status_code==200 and Path('/state/tokens.json').is_file():break
            except httpx.TransportError:pass
            await asyncio.sleep(.25)
        else:raise AssertionError('load_app_not_ready')
        unauthorized=await control.post('/mcp',json={});assert unauthorized.status_code==401
        tokens=json.loads(Path('/state/tokens.json').read_text());setup=time.monotonic()
        for i in range(1,21):
            client=httpx.AsyncClient(base_url='http://load-gateway:8000',timeout=45,trust_env=False,
                headers={'Authorization':'Bearer '+tokens[str(i)],'Accept':'application/json, text/event-stream'})
            clients.append(client)
            init=await rpc(client,{'id':i,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'synthetic-load','version':'1'}}})
            client.headers['MCP-Protocol-Version']=init['result']['protocolVersion'];await rpc(client,{'method':'notifications/initialized'})
        setup_seconds=round(time.monotonic()-setup,3)
        await reset(delay=.1);forbidden=await call(0,'unisender.sendEmail');assert forbidden['status']=='mcp_error' and P.stats()['calls']==0
        for n in [1,5,10,20]:
            await reset();begin=time.monotonic();values=await asyncio.gather(*(call(i) for i in range(n)));row=await record('burst_'+str(n),values,time.monotonic()-begin)
            assert set(v['status'] for v in values)<={200,429},('unexpected_burst_error',row)
            assert 1<=P.stats()['calls']<=n and P.stats()['peak_active']<=4
            if row['p95_ms']>5000:budget_failures.append({'case':row['name'],'p95_ms':row['p95_ms'],'budget_ms':5000})
            assert all(v['request_id_present'] for v in values)
        if os.getenv('LOAD_BURST_ONLY')=='1':
            final={'successful':not budget_failures,'all_bursts_completed':True,'latency_budget_failures':budget_failures,'cases':results,'diagnostic_only':True}
            Path('/results/load-results.json').write_text(json.dumps(final,indent=2));return
        await reset();begin=time.monotonic();values=await asyncio.gather(*(call(0) for _ in range(5)));row=await record('same_employee_5',values,time.monotonic()-begin)
        assert P.stats()['peak_active']<=2 and set(v['status'] for v in values)<={200,429}
        await reset(delay=.1);begin=time.monotonic();values=[]
        for i in range(10):
            values.append(await call(i, 'unisender.getContactCount'));await asyncio.sleep(max(0,begin+i+1-time.monotonic()))
        row=await record('paced_1_per_second',values,time.monotonic()-begin)
        assert all(v['status']==200 for v in values),('paced_status',row)
        if row['p95_ms']>2000:budget_failures.append({'case':row['name'],'p95_ms':row['p95_ms'],'budget_ms':2000})
        await reset(mode='failure',delay=0);begin=time.monotonic();values=[await call(0) for _ in range(4)]
        row=await record('service_failures_and_local_pause',values,time.monotonic()-begin)
        assert [v['status'] for v in values]==[502,502,502,503] and P.stats()['calls']==3
        # A local catalogue call remains usable during the provider pause.
        obj=await rpc(clients[0],{'id':999,'method':'tools/call','params':{'name':'gateway_access_profile','arguments':{}}})
        assert not obj['result'].get('isError')
        await asyncio.sleep(31);P.reset('probe',0)
        begin=time.monotonic();tasks=[asyncio.create_task(call(i)) for i in range(5)]
        for _ in range(80):
            if sum(t.done() for t in tasks)>=4:break
            await asyncio.sleep(.1)
        P.gate.set();values=await asyncio.gather(*tasks);row=await record('one_recovery_probe',values,time.monotonic()-begin)
        assert sum(v['status']==200 for v in values)==1 and sum(v['status']==503 for v in values)==4 and P.stats()['calls']==1,('probe_not_unique',row)
        await reset(mode='429',delay=0);begin=time.monotonic();values=[await call(0),await call(1)];row=await record('provider_429_cooldown',values,time.monotonic()-begin)
        assert [v['status'] for v in values]==[429,429] and P.stats()['calls']==1
        await asyncio.sleep(2.2);P.reset('ok',.1);assert (await call(2))['status']==200
        await reset(mode='trickle');begin=time.monotonic();value=await call(0);row=await record('overall_timeout_streaming', [value],time.monotonic()-begin)
        assert value['status']==504 and 25000<=value['ms']<=40000,('overall_timeout',row)
        for _ in range(25):
            if P.stats()['active']==0:break
            await asyncio.sleep(.2)
        assert P.stats()['active']==0,'fake_provider_did_not_drain'
        P.reset('ok',.1);assert (await call(1))['status']==200
        final={'successful':not budget_failures,'all_cases_completed':True,'safety_checks_passed':True,
            'latency_budget_failures':budget_failures,'setup_seconds':setup_seconds,'elapsed_seconds':round(time.monotonic()-started,3),
            'synthetic_identity':True,'real_oauth_tested':False,'real_provider_used':False,'provider_http':'private cleartext HTTP substitute',
            'policy_changed':False,'anonymous_mcp_status':401,'forbidden_method_denied':True,'local_profile_during_pause':True,
            'normal_burst_p95_budget_ms':5000,'paced_p95_budget_ms':2000,'cases':results}
        Path('/results/load-results.json').write_text(json.dumps(final,indent=2));print(json.dumps({'successful':not budget_failures,'cases':len(results)}),flush=True)
    finally:
        for client in clients:await client.aclose()
        await control.aclose();server.shutdown();server.server_close()

if __name__=='__main__':asyncio.run(run())
