"""Real disposable PostgreSQL: exact versions, ownership, expiry and client evidence."""
from dataclasses import replace
import hashlib,json,os
from unittest.mock import patch
from uuid import uuid4
from test_skill_lifecycle import LifecycleFixture
from test_skill_library import CONTENT
from gateway_mcp.services import skill_activity as activity,corporate_skills,telemetry,addon_guard,corporate_access,skill_lifecycle
from gateway_mcp.services.storage_core import _connect

class ActivityFixture(LifecycleFixture):
    def setUp(self):
        super().setUp()
        with _connect() as conn:conn.execute('truncate assistant_skill_events,assistant_sessions,audit_events restart identity')
        self.enterContext(patch.object(activity,'enabled',return_value=True))
        self.reader=replace(self.fresh(self.reader),scopes=('skills:read','telemetry:read','telemetry:write'))
        self.other=replace(self.fresh(self.other),scopes=('skills:read','telemetry:read','telemetry:write'))
        self.sid=self.draft()['name'];self.publish(self.sid)

    def read(self,actor=None):
        result=corporate_skills.read_skill(self.sid,actor=actor or self.reader)
        return result,activity.record_read(actor or self.reader,result)

    def event(self,read,rid,actor=None,**extra):
        return telemetry.record_skill_event(actor=actor or self.reader,event_type='started',agent='codex',client='codex-desktop',
            skill_id=self.sid,skill_pack=read['skill_pack'],skill_version=read['skill_version'],
            metadata_json=json.dumps({'skill_read_id':rid}),**extra)

class SkillActivityTests(ActivityFixture):
    def test_server_read_and_linked_event_are_distinct_and_own_only(self):
        read,r=self.read();self.assertTrue(r['recorded']);self.event(read,r['id'])
        rows=activity.own_activity(self.reader,skill=self.sid)['items']
        self.assertEqual({r['kind'] for r in rows},{'server_read','client_started'})
        self.assertTrue(all(r['read_link_verified'] and not r['execution_confirmed'] for r in rows))
        self.assertEqual({r['sha256'] for r in rows},{hashlib.sha256(CONTENT).hexdigest()})
        self.assertEqual(activity.own_activity(self.other)['items'],[])

    def test_foreign_expired_wrong_version_and_fabricated_ids_write_no_events(self):
        read,r=self.read()
        for actor,item,identifier in [(self.other,read,r['id']),(self.reader,{**read,'skill_version':'2'},r['id']),
                                      (self.reader,read,'sr-'+'f'*32)]:
            with self.assertRaises(PermissionError):self.event(item,identifier,actor)
        with _connect() as conn:
            conn.execute("update audit_events set created_at=now()-interval '25 hours' where event='skill_resource_read'")
        with self.assertRaises(PermissionError):self.event(read,r['id'])
        with _connect() as conn:self.assertEqual(conn.execute('select count(*) n from assistant_skill_events').fetchone()['n'],0)

    def test_link_survives_new_publication_and_archive_without_granting_new_read(self):
        read,r=self.read()
        skill_lifecycle.update_draft(self.reader,skill_id=self.sid,expected_revision=self.row(self.sid)['revision'],title='Changed',version='1.0.1',filename='SKILL.md',content=CONTENT+b'\nChanged material.\n',bundled=self.bundled)
        self.publish(self.sid);self.action(self.sid,'archive')
        linked=self.event(read,r['id']);self.assertTrue(linked['event']['read_link_verified'])
        self.assertEqual(linked['event']['skill_version'],'1.0.0')
        with self.assertRaises(PermissionError):corporate_skills.read_skill(self.sid,actor=self.reader)

    def test_historical_proof_remains_visible_after_new_link_window(self):
        read,r=self.read();self.event(read,r['id'])
        with _connect() as conn:conn.execute("update audit_events set created_at=now()-interval '25 hours' where event='skill_resource_read'")
        client=next(x for x in activity.own_activity(self.reader)['items'] if x['kind']=='client_started')
        self.assertTrue(client['read_link_verified'])

    def test_events_without_client_support_are_explicitly_unlinked(self):
        telemetry.record_skill_event(actor=self.reader,event_type='completed',agent='opencode',skill_id=self.sid,skill_version='1.0.0')
        row=activity.own_activity(self.reader)['items'][0]
        self.assertEqual(row['kind'],'client_completed');self.assertFalse(row['read_link_verified']);self.assertFalse(row['execution_confirmed'])

    def test_deduplicated_event_does_not_replace_version_or_link(self):
        read,r=self.read();correlation=str(uuid4())
        telemetry.record_skill_event(actor=self.reader,event_type='started',agent='codex',skill_id=self.sid,skill_version='old',correlation_id=correlation)
        result=self.event(read,r['id'],correlation_id=correlation)
        self.assertTrue(result['duplicate']);self.assertFalse(result['event']['read_link_verified']);self.assertEqual(result['event']['skill_version'],'old')

    def test_no_telemetry_scope_and_invalid_filter_are_denied(self):
        with self.assertRaises(PermissionError):activity.own_activity(replace(self.reader,scopes=()))
        for value in ['../secret',"' OR 1=1",'<script>']:
            with self.assertRaises(ValueError):activity.own_activity(self.reader,skill=value)

    def test_corporate_guard_links_receipt_and_only_labels_permitted_real_routes(self):
        read,r=self.read();args={'agent':'codex','client':'codex-desktop','project':'platforma-training','skill_id':self.sid,
            'skill_pack':read['skill_pack'],'skill_version':read['skill_version'],'metadata_json':json.dumps({'skill_read_id':r['id']}),
            'mcp_routes_json':'["unisender.getLists"]'}
        profile=json.loads(self.profile_path.read_text())['profiles'][self.owner.subject]
        profile['skill_telemetry']={'agents':['codex'],'clients':['codex-desktop'],'project':'platforma-training','skills':{},'scopes':[]}
        profile['resources']+=[{'id':'unisender.getLists','kind':'backend_route','operations':['read']}]
        with patch.object(addon_guard,'profile_for',return_value=profile),patch.object(addon_guard,'_check'),patch.object(corporate_access,'enabled',return_value=True):
            addon_guard.check_call(self.reader,'gateway_telemetry_skill_started',args)
            for metadata in [{'skill_read_id':r['id'],'execution_confirmed':True},{'prompt':'secret'}]:
                with self.assertRaises(PermissionError):addon_guard.check_call(self.reader,'gateway_telemetry_skill_started',{**args,'metadata_json':json.dumps(metadata)})
            with self.assertRaises(PermissionError):addon_guard.check_call(self.other,'gateway_telemetry_skill_started',args)
            with self.assertRaises(PermissionError):addon_guard.check_call(self.reader,'gateway_telemetry_skill_started',{**args,'mcp_routes_json':'["unisender.sendEmail"]'})

class CorporateSkillProtocolTests(ActivityFixture):
    def test_real_corporate_http_preflight_read_link_and_private_browser_timeline(self):
        from starlette.testclient import TestClient
        from gateway_mcp.services import bitrix_identity as identity,auth
        member='a'*32;subject='bitrix24:'+member+':42'
        self.enterContext(patch.dict(os.environ,{'GATEWAY_LOGIN_PROVIDER':'bitrix24','GATEWAY_BITRIX_PORTAL':'akmetron.bitrix24.ru',
            'GATEWAY_BITRIX_CLIENT_ID':'local.synthetic.12345678','GATEWAY_BITRIX_CLIENT_SECRET':'synthetic-application-secret',
            'GATEWAY_BITRIX_MEMBER_ID':member,'GATEWAY_BITRIX_OWNER_USER_ID':'3318'}))
        self.enterContext(patch.object(identity,'current_employee',return_value={'subject':subject,'user_id':'42','member_id':member,'name':'Synthetic Employee','verified_at':0}))
        self.enterContext(patch.object(identity,'_request',side_effect=AssertionError('NO_EXTERNAL_API')))
        cfg=json.loads(self.profile_path.read_text());cfg['profiles'][self.owner.subject]['skill_telemetry']={
            'project':'platforma-training','skills':{},'agents':['codex'],'clients':['codex-desktop'],'scopes':['telemetry:read','telemetry:write']}
        self.profile_path.write_text(json.dumps(cfg))
        actor=corporate_access.delegated(corporate_access.fresh_actor(subject))
        headers={'Authorization':'Bearer '+auth.mint_gateway_token(actor),'Accept':'application/json, text/event-stream'}
        with TestClient(self.mcp_test_server().streamable_http_app(),base_url='https://gateway.example.invalid') as http:
            seq=0
            def rpc(method,params=None):
                nonlocal seq
                seq+=1;response=http.post('/mcp',json={'jsonrpc':'2.0','id':seq,'method':method,'params':params or {}},headers=headers)
                self.assertEqual(response.status_code,200,response.text[:400])
                if response.headers.get('mcp-session-id'):headers['Mcp-Session-Id']=response.headers['mcp-session-id']
                if 'text/event-stream' in response.headers.get('content-type',''):
                    return next(json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:') and '"result"' in line)
                return response.json()
            init=rpc('initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'isolated-corporate-skill','version':'1'}})
            headers['MCP-Protocol-Version']=init['result']['protocolVersion']
            http.post('/mcp',json={'jsonrpc':'2.0','method':'notifications/initialized'},headers=headers)
            schema=next(t for t in rpc('tools/list')['result']['tools'] if t['name']=='gateway_corporate_skill_read')
            self.assertIn('check_only',schema['inputSchema']['properties'])
            def tool(name,args):
                value=rpc('tools/call',{'name':name,'arguments':args})['result']
                self.assertFalse(value.get('isError'),value)
                return json.loads(next(x['text'] for x in value['content'] if x['type']=='text'))
            checked=tool('gateway_corporate_skill_read',{'skill':self.sid,'check_only':True})
            self.assertEqual(checked['requirements_source'],'requirements_missing');self.assertNotIn('content',checked)
            self.assertEqual(activity.own_activity(actor)['items'],[])
            read=tool('gateway_corporate_skill_read',{'skill':self.sid})
            self.assertTrue(read['read_receipt']['recorded'])
            event=tool('gateway_telemetry_skill_started',{'agent':'codex','client':'codex-desktop','project':'platforma-training',
                'skill_id':self.sid,'skill_pack':read['skill_pack'],'skill_version':read['skill_version'],
                'metadata_json':json.dumps({'skill_read_id':read['read_receipt']['id']})})
            self.assertTrue(event['event']['read_link_verified'])
            browser=corporate_access.fresh_actor(subject)
            http.cookies.set('gateway_token',auth.mint_gateway_token(browser),domain='gateway.example.invalid',path='/')
            page=http.get('/platforma/skills/activity');self.assertEqual(page.status_code,200,page.text[:400])
            self.assertIn('Сервер: прочитан SKILL.md',page.text);self.assertIn('Клиент: сообщил о начале',page.text)
            self.assertNotIn(self.owner.subject,page.text)
            self.assertEqual(http.get('/platforma/skills/activity?actor_subject=foreign').status_code,409)
