"""Integration checks for an explicitly disposable PostgreSQL, never production."""
import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

assert os.getenv('SANDBOX_DISPOSABLE_DATABASE')=='true'
assert 'sandbox_feature_test' in os.environ['GATEWAY_DATABASE_URL']
from gateway_mcp.services.migrations import migrate_up
from gateway_mcp.services import addon_guard, storage_telemetry
from gateway_mcp.services.policy import GatewayActor, has_scope
from gateway_mcp.services.access_admin import admin_grant_resource
from gateway_mcp.tools.router import register_router_tools
from gateway_mcp.tools.telemetry import register_telemetry_tools
from gateway_mcp.backends.platforma_demo_store import DATA_FILE
from gateway_mcp.routes import admin_telemetry
from starlette.requests import Request
from starlette.applications import Starlette
from starlette.routing import Route
import httpx
from gateway_mcp.routes import platforma_requests as request_ui
from gateway_mcp.services import platforma_onboarding as onboarding

migrate_up()
OWNER='yandex:9001'
actor=GatewayActor(subject=OWNER,scopes=('tools:call','tools:read','platforma:read','platforma:write','telemetry:read','telemetry:write'))
class Mcp:
    def __init__(self):self.tools={};self.routes={}
    def tool(self,**kwargs):
        def register(fn):self.tools[fn.__name__]=fn;return fn
        return register
    def custom_route(self,path,**kwargs):
        def register(fn):self.routes[path]=fn;return fn
        return register
mcp=Mcp();register_router_tools(mcp);register_telemetry_tools(mcp)
admin_telemetry.register_admin_telemetry_routes(mcp)
for pattern,kind,actions in [('*','',['read']),('sandbox','sandbox_requests',['write'])]:
    admin_grant_resource(subject_type='user',subject_key=OWNER,system='platforma',resource_pattern=pattern,
        resource_type=kind,actions_json=json.dumps(actions),effect='allow',priority=10,
        reason='Disposable integration test',actor=actor,ttl_days=1,dry_run=False)

profile_data=json.loads(Path('/candidate/candidate/profiles-after.json').read_text())
assert len(profile_data['profiles']) == 1
profile=next(iter(profile_data['profiles'].values()));profile['subject']=OWNER
os.environ['GATEWAY_PLATFORMA_OWNER_SUBJECT']=OWNER
sha=hashlib.sha256(DATA_FILE.read_bytes()).hexdigest()
checks=[]
def ok(name):checks.append(name)
def require(scope,tool=''):
    if not has_scope(actor,scope):raise PermissionError('Missing scope')
    return actor

async def invoke(name,**arguments):
    addon_guard.check_call(actor,name,arguments)
    return json.loads(await mcp.tools[name](**arguments))
async def backend(name,args=None,key=''):
    return await invoke('gateway_call_tool',tool_name=name,arguments_json=json.dumps(args or {}),idempotency_key=key)

async def main():
    with ExitStack() as stack:
        stack.enter_context(patch('gateway_mcp.tools.runtime.require_scope',side_effect=require))
        stack.enter_context(patch('gateway_mcp.tools.router.current_actor',return_value=actor))
        stack.enter_context(patch('gateway_mcp.backends.platforma_demo.current_actor',return_value=actor))
        stack.enter_context(patch.object(addon_guard,'profile_for',return_value=profile))
        context=await backend('platforma.context.get');assert context['data']['visible_requests']==9144
        page=await backend('platforma.requests.list',{'period':'2026-10','limit':5,'sort':'id'})
        assert len(page['data']['records'])==5;ok('five_read_routes_still_available')
        service=await backend('platforma.services.list',{'limit':1})
        await backend('platforma.services.get',{'service_id':service['data']['records'][0]['id']})
        template=page['data']['records'][0]['id']
        args={'template_request_id':template,'title':'Учебная интеграционная заявка','due_at':'2026-10-20'}
        key='create-'+uuid4().hex
        try:await backend('platforma.requests.create',args)
        except ValueError:ok('missing_idempotency_rejected')
        else:raise AssertionError('Missing key allowed')
        created=await backend('platforma.requests.create',args,key)
        record=created['data']['record'];identifier=record['id']
        replay=await backend('platforma.requests.create',args,key)
        assert replay['data']['record']['id']==identifier and replay['idempotency']['replayed']
        assert (await backend('platforma.context.get'))['data']['visible_requests']==9145
        ok('create_and_idempotent_replay_one_record')
        try:await backend('platforma.requests.create',{**args,'title':'Другой payload'},key)
        except (ValueError,PermissionError):ok('idempotency_payload_mismatch_rejected')
        else:raise AssertionError('Changed payload accepted')
        update={'request_id':identifier,'expected_revision':1,'changes':{'title':'Проверенная учебная заявка','priority':'Критичный'}}
        changed=await backend('platforma.requests.update',update,'update-'+uuid4().hex)
        reread=(await backend('platforma.requests.get',{'request_id':identifier}))['data']['record']
        assert reread['revision']==2 and reread['priority']=='Критичный';ok('update_then_readback')
        try:await backend('platforma.requests.update',update,'stale-'+uuid4().hex)
        except Exception as error:
            assert 'REVISION_CONFLICT' in str(error);ok('stale_update_does_not_overwrite')
        else:raise AssertionError('Stale update allowed')
        event={'agent':'codex','client':'codex-desktop','skill_id':'platforma-training-service-reader',
            'skill_pack':'platforma-training','skill_version':'0.1.0','project':'platforma-training',
            'session_id':'feature-test-'+uuid4().hex,'metadata_json':'{}'}
        started=await invoke('gateway_telemetry_skill_started',**event)
        assert started['stored'];correlation=started['event']['correlation_id']
        complete=dict(event,correlation_id=correlation,duration_ms=15,mcp_routes_json='["platforma.requests.list"]')
        assert (await invoke('gateway_telemetry_skill_completed',**complete))['stored']
        assert (await invoke('gateway_telemetry_skill_completed',**complete))['duplicate'];ok('started_completed_and_terminal_deduplication')
        failed=await invoke('gateway_telemetry_skill_started',**event)
        assert (await invoke('gateway_telemetry_skill_failed',**event,correlation_id=failed['event']['correlation_id'],error_class='SyntheticCheckError'))['stored']
        own=await invoke('gateway_telemetry_skill_stats',project='platforma-training')
        row=next(s for s in own['skills'] if s['skill_id']==event['skill_id'])
        assert row['started_count']==2 and row['completed_count']==1 and row['failed_count']==1
        ok('real_postgres_stats_two_starts_one_success_one_failure')
        try:await invoke('gateway_telemetry_skill_started',**{**event,'metadata_json':'{"prompt":"DO-NOT-STORE-CONTENT"}'})
        except PermissionError:ok('private_payload_rejected')
        else:raise AssertionError('Private content accepted')
        admin=GatewayActor(subject=OWNER,scopes=('access:admin',))
        with patch.object(admin_telemetry,'web_actor',return_value=admin):
            response=await mcp.routes['/admin/telemetry/skills'](Request({'type':'http','method':'GET','path':'/admin/telemetry/skills','query_string':b'project=platforma-training','headers':[]}))
        html=response.body.decode();assert response.status_code==200 and event['skill_id'] in html and 'Метрики навыков' in html
        Path('/tmp/skill-metrics-check.html').write_text(html)
        ok('panel_server_render_contains_real_skill_aggregation')
        foreign=GatewayActor(subject='yandex:9002',scopes=('telemetry:write',))
        session='bound-session-'+uuid4().hex
        kwargs=dict(session_id=session,actor_subject=OWNER,agent='codex',client='codex-desktop',project='platforma-training',metadata={})
        storage_telemetry.upsert_assistant_session(**kwargs)
        try:storage_telemetry.upsert_assistant_session(**{**kwargs,'actor_subject':foreign.subject})
        except PermissionError:ok('session_cannot_be_reassigned_to_foreign_actor')
        else:raise AssertionError('Session reassigned')
        # Browser reads the exact same overlay; only the browser identity is injected.
        # Resource grants, reader approval, profiles, SQL and ASGI GET/405 are real.
        os.environ['GATEWAY_PLATFORMA_ONBOARDING_ENABLED']='true'
        os.environ['GATEWAY_PLATFORMA_READER_UNTIL']='2026-10-31T20:59:59Z'
        os.environ['GATEWAY_BRAND']='akmetron'
        profile_file=Path('/tmp/browser-test-profiles.json')
        profile_file.write_text(json.dumps({'schema':1,'profiles':{OWNER:profile}}))
        os.environ['GATEWAY_ADDON_PROFILES_FILE']=str(profile_file)
        browser_actor=[actor]
        class BrowserRoutes:
            def __init__(self):self.routes=[]
            def custom_route(self,path,**kwargs):
                def register(fn):self.routes.append(Route(path,fn,methods=kwargs['methods']));return fn
                return register
        browser=BrowserRoutes();request_ui.register_platforma_request_routes(browser)
        with ExitStack() as browser_stack:
            browser_stack.enter_context(patch.object(request_ui,'web_actor',side_effect=lambda request:browser_actor[0]))
            browser_stack.enter_context(patch.object(onboarding,'require_owner'))
            browser_stack.enter_context(patch.object(addon_guard,'profile_for',side_effect=lambda current:profile if current.subject==OWNER else onboarding.reader_profile(current)))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=browser.routes)),base_url='http://fixture') as client:
                response=await client.get(request_ui.LIST_PATH,params={'query':'Проверенная учебная заявка','period':'all'})
                assert response.status_code==200 and identifier in response.text and 'Найдено: 1</h2>' in response.text
                Path('/tmp/browser-requests-list.html').write_text(response.text)
                response=await client.get(request_ui.DETAIL_PATH,params={'id':identifier})
                assert response.status_code==200 and 'sandbox-overlay.json' in response.text and '<dd>2</dd>' in response.text
                Path('/tmp/browser-request-card.html').write_text(response.text)
                ok('browser_list_and_card_read_same_mcp_created_updated_record')
                overlay=Path(os.environ['GATEWAY_PLATFORMA_SANDBOX_DATA_DIR'])/'requests.json'
                overlay_sha=hashlib.sha256(overlay.read_bytes()).hexdigest()
                for verb in ('POST','PATCH','DELETE'):
                    assert (await client.request(verb,request_ui.LIST_PATH)).status_code==405
                    assert (await client.request(verb,request_ui.DETAIL_PATH,params={'id':identifier})).status_code==405
                assert hashlib.sha256(overlay.read_bytes()).hexdigest()==overlay_sha
                ok('asgi_rejects_all_browser_mutations_without_changing_overlay')
                colleague=GatewayActor(subject='yandex:9002',yandex_id='9002',email='reader@example.invalid',login='reader',scopes=())
                browser_actor[0]=colleague
                assert (await client.get(request_ui.LIST_PATH)).status_code==403
                pending=onboarding.own_request(colleague,create=True)
                approved=onboarding.decide(actor,str(pending['id']),'approved',pending['revision'],'Disposable browser reader test')
                assert onboarding.check_web_path(colleague,request_ui.LIST_PATH)
                response=await client.get(request_ui.LIST_PATH,params={'query':'Проверенная учебная заявка','period':'all'})
                assert response.status_code==200 and identifier in response.text
                for private in ('/admin/audit','/admin/telemetry/skills','/credentials','/notifications'):
                    assert 'href="'+private not in response.text
                    assert not onboarding.check_web_path(colleague,private)
                ok('real_database_reader_approval_opens_only_readonly_browser_views')
                onboarding.decide(actor,str(approved['id']),'revoked',approved['revision'],'Disposable revocation test')
                assert (await client.get(request_ui.DETAIL_PATH,params={'id':identifier})).status_code==403
                assert not onboarding.check_web_path(colleague,request_ui.LIST_PATH)
                ok('reader_revocation_closes_existing_browser_identity_immediately')
        assert hashlib.sha256(DATA_FILE.read_bytes()).hexdigest()==sha
        ok('original_9144_snapshot_bytes_unchanged')
    print(json.dumps({'ok':True,'checks':checks,'count':len(checks),'disposable_database':True,'oauth_or_live_browser_tested':False},ensure_ascii=False))

asyncio.run(main())
