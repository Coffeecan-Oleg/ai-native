"""Real disposable PostgreSQL; no production actors or database."""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from test_reader_onboarding import ReaderFixture
from gateway_mcp.services import platforma_onboarding as service, addon_guard
from gateway_mcp.services.oauth_limits import limit_oauth_actor
from gateway_mcp.services.storage_core import _connect
from gateway_mcp.services.policy import GatewayActor, has_scope
from gateway_mcp.tools.router import register_router_tools
from gateway_mcp.tools.telemetry import register_telemetry_tools
from gateway_mcp.backends.platforma_demo_store import DATA_FILE
import hashlib


class ParticipantPilotTests(ReaderFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ,{
            'GATEWAY_PLATFORMA_PARTICIPANT_PILOT_ENABLED':'true',
            'GATEWAY_PLATFORMA_SANDBOX_WRITE_ENABLED':'true',
            'GATEWAY_PLATFORMA_SANDBOX_DATA_DIR':str(self.tmp/'requests'),
            'GATEWAY_MCP_OAUTH_SCOPES':' '.join(sorted(service.SCOPES|service.PARTICIPANT_EXTRA_SCOPES)),
            'GATEWAY_MCP_OAUTH_TOOLS':' '.join(sorted(service.TOOLS|service.ROUTES|service.PARTICIPANT_TOOLS|service.PARTICIPANT_ROUTES))}))
        limits={'project':'platforma-training','skills':{'platforma-training-service-writer':{'pack':'platforma-training','version':'0.1.0'}},
                'agents':['codex'],'scopes':sorted(service.SCOPES|service.PARTICIPANT_EXTRA_SCOPES)}
        self.profiles['profiles']['yandex:9001']['skill_telemetry']=limits
        self.profile_path.write_text(json.dumps(self.profiles))

    def participant(self):
        self.approve()
        return limit_oauth_actor(self.fresh())

    def invoke(self,actor,name,**arguments):
        class Mcp:
            def __init__(self):self.tools={}
            def tool(self,**kw):
                def register(fn):self.tools[fn.__name__]=fn;return fn
                return register
        mcp=Mcp();register_router_tools(mcp);register_telemetry_tools(mcp)
        def require(scope,tool=''):
            if not has_scope(actor,scope):raise PermissionError('Missing scope')
            return actor
        async def call():
            addon_guard.check_call(actor,name,arguments)
            with patch('gateway_mcp.tools.runtime.require_scope',side_effect=require),\
                 patch('gateway_mcp.tools.router.current_actor',return_value=actor),\
                 patch('gateway_mcp.backends.platforma_demo.current_actor',return_value=actor):
                return json.loads(await mcp.tools[name](**arguments))
        return asyncio.run(call())

    def route(self,actor,name,args=None,key=''):
        return self.invoke(actor,'gateway_call_tool',tool_name=name,arguments_json=json.dumps(args or {}),idempotency_key=key)

    def test_pending_gets_no_participant_access(self):
        service.own_request(self.reader,create=True)
        self.assertIsNone(service.active_participant(self.reader.subject))
        with self.assertRaises(PermissionError):limit_oauth_actor(self.reader)

    def test_approved_gets_exact_nine_scopes_and_finite_routes(self):
        actor=self.participant()
        self.assertEqual(set(actor.scopes),service.SCOPES|service.PARTICIPANT_EXTRA_SCOPES)
        self.assertEqual(actor.groups,())
        self.assertEqual(self.counts(),(1,9,2))
        self.assertEqual(set(actor.tool_allowlist),service.TOOLS|service.ROUTES|service.PARTICIPANT_TOOLS|service.PARTICIPANT_ROUTES)
        self.assertNotIn('gateway_memory_search',actor.tool_allowlist)
        self.assertNotIn('gateway_usage_dashboard',actor.tool_allowlist)

    def test_legacy_upgrade_is_explicit_idempotent_and_does_not_extend_expiry(self):
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_PARTICIPANT_PILOT_ENABLED':'false'}):
            old=self.approve()
            old_actor=limit_oauth_actor(self.fresh())
        self.assertIsNone(service.active_participant(self.reader.subject))
        result=service.upgrade_approved_participants(self.owner)
        self.assertEqual(len(result['upgraded_request_ids']),1)
        self.assertEqual(self.counts(),(1,9,2))
        self.assertEqual(service.active_participant(self.reader.subject)['approved_until'],old['approved_until'])
        self.assertEqual(service.upgrade_approved_participants(self.owner)['upgraded_request_ids'],[])
        self.assertNotIn('platforma:write',limit_oauth_actor(old_actor).scopes)
        with self.assertRaises(PermissionError):service.upgrade_approved_participants(self.reader)

    def test_create_replay_update_and_readback_use_shared_synthetic_overlay(self):
        actor=self.participant();before=hashlib.sha256(DATA_FILE.read_bytes()).hexdigest()
        args={'template_request_id':'REQ-F-001','title':'PILOT fictional check','due_at':'2026-10-20','priority':'Обычный'}
        key=str(uuid4())
        created=self.route(actor,'platforma.requests.create',args,key);record=created['data']['record']
        self.assertEqual(record['revision'],1)
        replay=self.route(actor,'platforma.requests.create',args,key)
        self.assertTrue(replay['idempotency']['replayed']);self.assertEqual(replay['data']['record']['id'],record['id'])
        update={'request_id':record['id'],'expected_revision':1,'changes':{'priority':'Высокий'}}
        self.route(actor,'platforma.requests.update',update,str(uuid4()))
        got=self.route(actor,'platforma.requests.get',{'request_id':record['id']})['data']['record']
        self.assertEqual((got['revision'],got['priority']),(2,'Высокий'))
        with self.assertRaises(Exception) as conflict:self.route(actor,'platforma.requests.update',update,str(uuid4()))
        self.assertIn('REVISION_CONFLICT',str(conflict.exception))
        self.assertEqual(self.route(actor,'platforma.context.get')['data']['visible_requests'],9145)
        self.assertEqual(hashlib.sha256(DATA_FILE.read_bytes()).hexdigest(),before)

    def test_no_memory_private_space_other_stats_or_arbitrary_backend(self):
        actor=self.participant()
        for name,args in [
            ('gateway_memory_search',{'scope':'user','include_sources':False,'include_yonote':False}),
            ('gateway_usage_dashboard',{}),
            ('gateway_knowledge_document_get',{'space':'personal','document':'private.md'}),
            ('gateway_telemetry_skill_stats',{'actor_subject':self.owner.subject}),
            ('gateway_call_tool',{'tool_name':'arbitrary.real.crm.write'})]:
            with self.subTest(name=name):
                with self.assertRaises(PermissionError):addon_guard.check_call(actor,name,args)
        addon_guard.check_call(actor,'gateway_knowledge_document_get',{'space':service.SPACE,'document':'awareness/start.md'})
        self.assertFalse(service.check_web_path(actor,'/admin/telemetry/skills'))
        self.assertFalse(service.check_web_path(actor,'/credentials'))
        self.assertTrue(service.check_web_path(actor,'/platforma/requests'))

    def test_actual_claude_telemetry_and_own_stats(self):
        actor=self.participant()
        event={'agent':'claude','client':'claude-desktop','skill_id':'platforma-training-service-writer',
               'skill_pack':'platforma-training','skill_version':'0.1.0','project':'platforma-training',
               'session_id':'test-'+uuid4().hex,'metadata_json':'{}'}
        started=self.invoke(actor,'gateway_telemetry_skill_started',**event)
        self.assertTrue(started['stored'])
        self.invoke(actor,'gateway_telemetry_skill_completed',**event,correlation_id=started['event']['correlation_id'],duration_ms=100,mcp_routes_json='["platforma.requests.create"]')
        stats=self.invoke(actor,'gateway_telemetry_skill_stats',project='platforma-training',agent='claude')
        self.assertEqual(stats['skills'][0]['completed_count'],1)
        for change in [{'metadata_json':'{"prompt":"PRIVATE"}'},{'agent':'unknown'},{'client':'unknown'}]:
            with self.assertRaises(PermissionError):self.invoke(actor,'gateway_telemetry_skill_started',**(event|change))

    def test_chatgpt_client_is_bounded_and_no_session_impersonation(self):
        actor=self.participant()
        event={'agent':'chatgpt','client':'chatgpt','skill_id':'platforma-training-service-writer','skill_pack':'platforma-training','skill_version':'0.1.0','project':'platforma-training','metadata_json':'{}','session_id':'shared-'+uuid4().hex}
        self.invoke(actor,'gateway_telemetry_skill_started',**event)
        self.approve(self.other)
        other=limit_oauth_actor(self.fresh(self.other))
        with self.assertRaises(Exception):self.invoke(other,'gateway_telemetry_skill_started',**event)
        with self.assertRaises(PermissionError):self.invoke(other,'gateway_telemetry_skill_stats',project='platforma-training',actor_subject=actor.subject)

    def test_revocation_closes_existing_token_and_revokes_extra_grant(self):
        actor=self.participant();row=service.active_reader(actor.subject)
        service.decide(self.owner,row['id'],'revoked',row['revision'],'End test access')
        self.assertIsNone(service.active_participant(actor.subject))
        with self.assertRaises(PermissionError):self.route(actor,'platforma.requests.create',{},str(uuid4()))
        with _connect() as conn:
            self.assertEqual(conn.execute("select count(*) as n from access_resource_grants where reason=%s and revoked_at is null",(service._participant_reason(row),)).fetchone()['n'],0)

    def test_disabled_pilot_keeps_reads_and_closes_writes(self):
        actor=self.participant()
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_PARTICIPANT_PILOT_ENABLED':'false'}):
            self.assertIsNotNone(service.active_reader(actor.subject))
            self.assertIsNone(service.active_participant(actor.subject))
            with self.assertRaises(PermissionError):self.route(actor,'platforma.requests.create',{},str(uuid4()))
            self.assertEqual(self.route(actor,'platforma.context.get')['data']['visible_requests'],9144)

    def test_unapproved_actor_cannot_bypass_backend_guard(self):
        from gateway_mcp.backends.platforma_demo import _call_platforma_demo
        actor=GatewayActor(subject='yandex:9555',scopes=('platforma:write',))
        with patch('gateway_mcp.backends.platforma_demo.current_actor',return_value=actor):
            with self.assertRaises(PermissionError):asyncio.run(_call_platforma_demo({'name':'platforma.requests.create','scope':'platforma:write','http_method':'POST'},{}))

    def test_real_http_mcp_participant_create_update_stats_and_privacy(self):
        from starlette.testclient import TestClient
        from gateway_mcp.server import create_mcp
        from gateway_mcp.services.auth import mint_gateway_token
        actor=self.participant()
        with TestClient(create_mcp().streamable_http_app(),base_url='https://gateway.example.invalid') as http:
            headers={'Authorization':'Bearer '+mint_gateway_token(actor),'Accept':'application/json, text/event-stream'}
            seq=10
            def rpc(body):
                response=http.post('/mcp',json={'jsonrpc':'2.0',**body},headers=headers)
                self.assertIn(response.status_code,(200,202),response.text[:300])
                if response.headers.get('mcp-session-id'):headers['Mcp-Session-Id']=response.headers['mcp-session-id']
                if not response.content:return {}
                if 'text/event-stream' in response.headers.get('content-type',''):
                    return next(json.loads(s[5:].strip()) for s in response.text.splitlines() if s.startswith('data:') and any(k in s for k in ['"result"','"error"']))
                return response.json()
            init=rpc({'id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'isolated-participant','version':'1'}}})
            headers['MCP-Protocol-Version']=init['result']['protocolVersion'];rpc({'method':'notifications/initialized'})
            def tool(name,args):
                nonlocal seq
                seq+=1
                result=rpc({'id':seq,'method':'tools/call','params':{'name':name,'arguments':args}})['result']
                value=next(x['text'] for x in result['content'] if x['type']=='text')
                return result,json.loads(value)
            tools=rpc({'id':2,'method':'tools/list'})['result']['tools']
            names={t['name'] for t in tools}
            self.assertIn('gateway_telemetry_skill_started',names);self.assertNotIn('gateway_memory_search',names)
            result,created=tool('gateway_call_tool',{'tool_name':'platforma.requests.create','arguments_json':json.dumps({'template_request_id':'REQ-F-001','title':'HTTP MCP participant check','due_at':'2026-10-20'}),'idempotency_key':str(uuid4())})
            self.assertFalse(result.get('isError'),created);identifier=created['data']['record']['id']
            result,updated=tool('gateway_call_tool',{'tool_name':'platforma.requests.update','arguments_json':json.dumps({'request_id':identifier,'expected_revision':1,'changes':{'priority':'Высокий'}}),'idempotency_key':str(uuid4())})
            self.assertFalse(result.get('isError'),updated);self.assertEqual(updated['data']['record']['revision'],2)
            result,_=tool('gateway_telemetry_skill_stats',{'project':'platforma-training','actor_subject':self.owner.subject})
            self.assertTrue(result.get('isError'))
