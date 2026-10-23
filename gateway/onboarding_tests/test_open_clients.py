"""Server contract tests using synthetic identities, not installed client apps."""
import json
import os
import time
from unittest.mock import patch
from uuid import uuid4

import test_participant_pilot as participant
from test_reader_onboarding import ReaderFixture
from gateway_mcp.services import auth, addon_guard, platforma_onboarding as service
from gateway_mcp.services.oauth_limits import limit_oauth_actor
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.services.storage_core import _connect


class OpenClientContracts(ReaderFixture):
    invoke=participant.ParticipantPilotTests.invoke
    route=participant.ParticipantPilotTests.route
    participant=participant.ParticipantPilotTests.participant

    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ,{
            'GATEWAY_PLATFORMA_PARTICIPANT_PILOT_ENABLED':'true',
            'GATEWAY_PLATFORMA_SANDBOX_WRITE_ENABLED':'true',
            'GATEWAY_PLATFORMA_SANDBOX_DATA_DIR':str(self.tmp/'requests'),
            'GATEWAY_MCP_OAUTH_SCOPES':' '.join(sorted(service.SCOPES|service.PARTICIPANT_EXTRA_SCOPES)),
            'GATEWAY_MCP_OAUTH_TOOLS':' '.join(sorted(service.TOOLS|service.ROUTES|service.PARTICIPANT_TOOLS|service.PARTICIPANT_ROUTES))}))
        self.profiles['profiles']['yandex:9001']['skill_telemetry']={
            'project':'platforma-training','agents':['codex'],'clients':['codex-desktop'],
            'skills':{'platforma-training-service-writer':{'pack':'platforma-training','version':'0.1.0'}},
            'scopes':sorted(service.SCOPES|service.PARTICIPANT_EXTRA_SCOPES)}
        self.profile_path.write_text(json.dumps(self.profiles))
        self._event_sessions=[]
        self.addCleanup(self._cleanup_events)

    def _cleanup_events(self):
        with _connect() as conn:
            self.assertEqual(conn.info.dbname,'platforma_onboarding_test')
            conn.execute('delete from assistant_skill_events where session_id=any(%s)',(self._event_sessions,))
            conn.execute('delete from assistant_sessions where session_id=any(%s)',(self._event_sessions,))

    def event(self,client):
        session='isolated-'+client+'-'+uuid4().hex
        self._event_sessions.append(session)
        return dict(agent=client,client=client,skill_id='platforma-training-service-writer',
                    skill_pack='platforma-training',skill_version='0.1.0',project='platforma-training',
                    session_id=session,metadata_json='{}')

    def test_both_clients_record_lifecycle_failure_and_own_statistics(self):
        actor=self.participant()
        for client in ('opencode','openclaw'):
            with self.subTest(client=client):
                event=self.event(client)
                started=self.invoke(actor,'gateway_telemetry_skill_started',**event)
                self.assertTrue(started['stored'])
                self.invoke(actor,'gateway_telemetry_skill_completed',**event,
                    correlation_id=started['event']['correlation_id'],duration_ms=30,mcp_routes_json='[]')
                failed_event=self.event(client)
                started=self.invoke(actor,'gateway_telemetry_skill_started',**failed_event)
                self.invoke(actor,'gateway_telemetry_skill_failed',**failed_event,
                    correlation_id=started['event']['correlation_id'],error_class='synthetic_test',duration_ms=10)
                stats=self.invoke(actor,'gateway_telemetry_skill_stats',project='platforma-training',agent=client)
                skill=stats['skills'][0]
                self.assertEqual((skill['started_count'],skill['completed_count'],skill['failed_count']),(2,1,1))

    def test_new_labels_do_not_allow_unknown_apps_content_or_private_access(self):
        actor=self.participant();event=self.event('opencode')
        for change in ({'agent':'unknown'},{'client':'unknown'},{'metadata_json':'{"prompt":"PRIVATE"}'},
                       {'skill_id':'unknown'},{'skill_version':'wrong'}):
            with self.subTest(change=change),self.assertRaises(PermissionError):
                self.invoke(actor,'gateway_telemetry_skill_started',**(event|change))
        for name,args in [('gateway_memory_search',{}),('gateway_usage_dashboard',{}),
                          ('gateway_telemetry_skill_stats',{'actor_subject':self.owner.subject}),
                          ('gateway_call_tool',{'tool_name':'real.crm.write'})]:
            with self.subTest(name=name),self.assertRaises(PermissionError):addon_guard.check_call(actor,name,args)
        self.assertNotIn('access:admin',actor.scopes)

    def test_old_reader_scope_ceiling_is_not_promoted_by_new_client_label(self):
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_PARTICIPANT_PILOT_ENABLED':'false'}):
            self.approve();old=limit_oauth_actor(self.fresh())
        service.upgrade_approved_participants(self.owner)
        limited=limit_oauth_actor(old)
        self.assertNotIn('telemetry:write',limited.scopes)
        for client in ('opencode','openclaw'):
            with self.subTest(client=client),self.assertRaises(PermissionError):
                self.invoke(limited,'gateway_telemetry_skill_started',**self.event(client))

    def test_rotating_refresh_preserves_nine_scopes_and_revocation(self):
        self.participant()
        for client in ('opencode','openclaw'):
            tokens=auth._issue_oauth_tokens(actor=self.fresh(),client_id='isolated-'+client,resource=auth.resource_url())
            for _ in range(2):
                previous=tokens['refresh_token']
                tokens=auth.exchange_mcp_refresh_token(refresh_token=previous,client_id='isolated-'+client,resource=auth.resource_url())
                self.assertEqual(set(tokens['scope'].split()),service.SCOPES|service.PARTICIPANT_EXTRA_SCOPES)
                with self.assertRaises(ValueError):auth.exchange_mcp_refresh_token(refresh_token=previous,client_id='isolated-'+client,resource=auth.resource_url())
        row=service.active_reader(self.reader.subject)
        service.decide(self.owner,row['id'],'revoked',row['revision'],'Isolated end of test')
        with self.assertRaises(PermissionError):
            auth.exchange_mcp_refresh_token(refresh_token=tokens['refresh_token'],client_id='isolated-openclaw',resource=auth.resource_url())

    def test_streamable_http_both_labels_tools_telemetry_and_request_roundtrip(self):
        from starlette.testclient import TestClient
        from gateway_mcp.server import create_mcp
        actor=self.participant()
        with TestClient(create_mcp().streamable_http_app(),base_url='https://gateway.example.invalid') as http:
            for client in ('opencode','openclaw'):
                headers={'Authorization':'Bearer '+auth.mint_gateway_token(actor),'Accept':'application/json, text/event-stream'}
                seq=10
                def rpc(body):
                    response=http.post('/mcp',json={'jsonrpc':'2.0',**body},headers=headers)
                    self.assertIn(response.status_code,(200,202),response.text[:300])
                    if response.headers.get('mcp-session-id'):headers['Mcp-Session-Id']=response.headers['mcp-session-id']
                    if not response.content:return {}
                    if 'text/event-stream' in response.headers.get('content-type',''):
                        return next(json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:') and any(k in line for k in ['"result"','"error"']))
                    return response.json()
                init=rpc({'id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':client,'version':'synthetic-contract-test'}}})
                headers['MCP-Protocol-Version']=init['result']['protocolVersion'];rpc({'method':'notifications/initialized'})
                def tool(name,args):
                    nonlocal seq
                    seq+=1
                    result=rpc({'id':seq,'method':'tools/call','params':{'name':name,'arguments':args}})['result']
                    self.assertFalse(result.get('isError'),str(result)[:350])
                    return json.loads(next(c['text'] for c in result['content'] if c['type']=='text'))
                names={t['name'] for t in rpc({'id':2,'method':'tools/list'})['result']['tools']}
                self.assertIn('gateway_telemetry_skill_completed',names);self.assertNotIn('gateway_memory_search',names)
                event=self.event(client)
                started=tool('gateway_telemetry_skill_started',event)
                created=tool('gateway_call_tool',{'tool_name':'platforma.requests.create','arguments_json':json.dumps({'template_request_id':'REQ-F-001','title':'Synthetic '+client,'created_at':'2026-10-14','due_at':'2026-10-20'}),'idempotency_key':str(uuid4())})['data']['record']
                request_id=created['id']
                for revision,changes in [(1,{'priority':'Высокий'}),(2,{'accepted_at':'2026-10-15'})]:
                    row=tool('gateway_call_tool',{'tool_name':'platforma.requests.update','arguments_json':json.dumps({'request_id':request_id,'expected_revision':revision,'changes':changes}),'idempotency_key':str(uuid4())})['data']['record']
                self.assertEqual((row['revision'],row['status']),(3,'early'))
                read=tool('gateway_call_tool',{'tool_name':'platforma.requests.get','arguments_json':json.dumps({'request_id':request_id})})['data']['record']
                self.assertEqual(read['id'],request_id)
                tool('gateway_telemetry_skill_completed',event|{'correlation_id':started['event']['correlation_id'],'duration_ms':20,'mcp_routes_json':'["platforma.requests.create","platforma.requests.update","platforma.requests.get"]'})
                stats=tool('gateway_telemetry_skill_stats',{'project':'platforma-training','agent':client})
                self.assertEqual(stats['skills'][0]['completed_count'],1)
