"""Synthetic-only wrapper around an unchanged installed Gateway image."""
import functools,json,logging,os,secrets,threading,time
from pathlib import Path
from urllib.parse import urlsplit

def main():
    assert os.getenv('PLATFORMA_LOAD_LAB')=='disposable-v1'
    assert urlsplit(os.environ['GATEWAY_DATABASE_URL']).path=='/gateway_load_test'
    assert Path('/usr/local/share/gateway-release/source-revision').read_text().strip()==os.environ['LOAD_SOURCE_SHA']
    if Path('/lab/overlay').is_dir():
        import gateway_mcp.services as services
        services.__path__.insert(0,'/lab/overlay')
    logging.disable(logging.CRITICAL)
    metrics={};metrics_lock=threading.Lock()
    def measure(module,name,label):
        original=getattr(module,name)
        @functools.wraps(original)
        def wrapped(*args,**kwargs):
            begin=time.perf_counter()
            try:return original(*args,**kwargs)
            finally:
                with metrics_lock:
                    count,total=metrics.get(label,(0,0));metrics[label]=(count+1,total+(time.perf_counter()-begin)*1000)
        setattr(module,name,wrapped)
    import psycopg
    measure(psycopg,'connect','database_connect')
    from gateway_mcp.services import migrations,bitrix_identity as identity,corporate_access as corporate,auth
    from gateway_mcp.backends import unisender_readonly as unisender
    from gateway_mcp.services.storage_core import _connect
    from gateway_mcp.services.platforma_onboarding import DOCUMENTS
    migrations.migrate_up()
    for module,name,label in [(corporate,'fresh_actor','corporate_fresh_actor'),(corporate,'profile','corporate_profile'),(auth,'current_actor','current_actor')]:
        measure(module,name,label)
    member='a'*32
    def employee(subject):
        portal,uid=identity.parse_subject(subject)
        if portal!=member or not 1<=int(uid)<=20:raise PermissionError('synthetic_employee_only')
        return {'user_id':uid,'name':'Synthetic '+uid,'verified_at':time.time()}
    identity.current_employee=employee
    def forbidden(*args,**kwargs):raise AssertionError('identity_network_forbidden_in_load_lab')
    identity._request=forbidden
    unisender.API_ROOT='http://load-driver:9000/ru/api/'
    unisender._credential=lambda:'synthetic-load-key-not-real'
    profile={'schema':1,'profiles':{'load-template':{'documents':{corporate.SPACE:{p:{} for p in DOCUMENTS}},'skill_telemetry':{}}}}
    Path('/state/profiles.json').write_text(json.dumps(profile))
    tokens={str(i):auth.mint_gateway_token(corporate.delegated(corporate.fresh_actor('bitrix24:'+member+':'+str(i))),ttl_seconds=600) for i in range(1,21)}
    tokenfile=Path('/state/tokens.json');tokenfile.write_text(json.dumps(tokens));tokenfile.chmod(0o600)
    from gateway_mcp.server import mcp as server
    from starlette.responses import JSONResponse
    if server.settings.transport_security is not None:
        server.settings.transport_security.allowed_hosts.append('load-gateway:8000')
        server.settings.transport_security.allowed_origins.append('http://load-gateway:8000')
    @server.custom_route('/__load__/control',methods=['POST'],include_in_schema=False)
    async def control(request):
        if not secrets.compare_digest(request.headers.get('x-load-control',''),os.environ['LOAD_CONTROL']):return JSONResponse({},status_code=403)
        data=await request.json();action=data.get('action')
        with _connect() as conn:
            assert conn.info.dbname=='gateway_load_test'
            slots=conn.execute('select count(*) n from gateway_external_calls').fetchone()['n']
            if action=='reset':
                if slots:return JSONResponse({'error':'slots_still_active'},status_code=409)
                conn.execute('truncate gateway_external_calls,gateway_external_circuits,gateway_external_limits')
            elif action!='state':return JSONResponse({},status_code=400)
            state=conn.execute('select failures,opened_until is not null as has_pause from gateway_external_circuits').fetchall()
            audit=conn.execute("select status,count(*) n from audit_events where event='tool_call' group by status").fetchall()
        with metrics_lock:
            if action=='reset':metrics.clear()
            snapshot={key:{'calls':v[0],'inclusive_ms':round(v[1],2)} for key,v in metrics.items()}
        return JSONResponse({'slots':slots,'circuits':state,'audit':audit,'diagnostics':snapshot})
    import uvicorn
    uvicorn.run(server.streamable_http_app(),host='0.0.0.0',port=8000,log_level='error',access_log=False)

if __name__=='__main__':main()
