import asyncio
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from starlette.requests import Request
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.services import connection_diagnostics as service
from gateway_mcp.routes import connection_diagnostics as routes


def actor(subject='employee:one',scopes=('skills:read','tools:read')):
    return GatewayActor(subject=subject,login='Test employee',scopes=scopes)


class FakeMCP:
    def __init__(self):self.routes={};self.tools={}
    def custom_route(self,path,**kwargs):
        def wrap(function):self.routes[path]=function;return function
        return wrap
    def tool(self,**kwargs):
        def wrap(function):self.tools[function.__name__]=function;return function
        return wrap


class DiagnosticServiceTests(unittest.TestCase):
    def test_marks_are_unique_and_expire(self):
        first,second=service.new_mark(),service.new_mark()
        self.assertNotEqual(first,second)
        self.assertEqual(service.validate_mark(first),first)
        with self.assertRaisesRegex(ValueError,'expired'):
            service.validate_mark(first,now=int(first.split('-')[1])+901)

    def test_future_and_malformed_marks_rejected(self):
        for value in ['',None,'Bearer SECRET','<script>','diag-0000000000-'+'a'*24,
                      'diag-'+str(int(time.time())+120)+'-'+'b'*24]:
            with self.subTest(value=value),self.assertRaises(ValueError):service.validate_mark(value)

    def test_untrusted_client_labels_and_versions_are_not_saved(self):
        mcp=SimpleNamespace(get_context=lambda:SimpleNamespace(session=SimpleNamespace(client_params=
            SimpleNamespace(clientInfo=SimpleNamespace(name='Bearer PRIVATE_KEY',version='secret-value')))))
        self.assertEqual(service.client_info(mcp),{'name':'unknown','version':''})

    def test_known_client_is_a_label_not_an_authorization_rule(self):
        mcp=SimpleNamespace(get_context=lambda:SimpleNamespace(session=SimpleNamespace(client_params=
            SimpleNamespace(clientInfo=SimpleNamespace(name='OpenCode',version='1.2.3')))))
        self.assertEqual(service.client_info(mcp),{'name':'opencode','version':'1.2.3'})
        self.assertEqual(service.client_info(FakeMCP()),{'name':'unknown','version':''})

    def test_probe_records_only_current_actor_and_effective_tools(self):
        current=GatewayActor(subject='employee:one',login='PRIVATE LOGIN',email='private@example.test',
             scopes=('skills:read','knowledge:read'),tool_allowlist=('gateway_access_profile',))
        with patch.object(service,'append_audit_event',return_value=True) as write:
            result=service.record_probe(current,service.new_mark(),FakeMCP())
        payload=write.call_args.args[0]
        self.assertEqual(payload['actor'],{'subject':'employee:one'})
        self.assertEqual(payload['arguments']['allowed_tools'],['gateway_access_profile'])
        self.assertNotIn('private@example.test',json.dumps(payload))
        self.assertNotIn('PRIVATE LOGIN',json.dumps(payload))
        self.assertTrue(result['recorded'])
        self.assertEqual(payload['channel'],'mcp')

    def test_scope_and_tool_ceiling_both_apply(self):
        current=GatewayActor(subject='employee:one',scopes=('skills:read',),
                             tool_allowlist=('gateway_access_profile','gateway_knowledge_search'))
        with patch.object(service,'append_audit_event',return_value=True) as write:
            service.record_probe(current,service.new_mark(),FakeMCP())
        self.assertEqual(write.call_args.args[0]['arguments']['allowed_tools'],['gateway_access_profile'])

    def test_storage_failure_does_not_claim_recorded(self):
        with patch.object(service,'append_audit_event',side_effect=RuntimeError('PRIVATE DSN')):
            result=service.record_probe(actor(),service.new_mark(),FakeMCP())
        self.assertFalse(result['recorded'])
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_anonymous_probe_and_read_are_denied_before_storage(self):
        for function,args in [(service.record_probe,(actor('anonymous'),service.new_mark(),FakeMCP())),
                              (service.read_probe,(actor('anonymous'),service.new_mark()))]:
            with self.subTest(function=function.__name__),self.assertRaises(PermissionError):function(*args)

    def test_database_lookup_is_subject_bound_and_does_not_return_payload(self):
        mark=service.new_mark();conn=MagicMock();cursor=conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value={'created_at':'2026-11-04T10:00:00+00:00','details':{
            'gateway_request_id':'gwc-'+'a'*32,'effective_scopes':['skills:read'],
            'allowed_tools':['gateway_access_profile','secret.nonpublic.tool'],
            'client':{'name':'cursor','version':'2.3.4'},'password':'PRIVATE','actor':'OTHER'}}
        with patch.object(service,'postgres_enabled',return_value=True),patch.object(service,'ensure_schema'),\
             patch.object(service,'_connect') as connect:
            connect.return_value.__enter__.return_value=conn
            result=service.read_probe(actor(),mark)
        sql,params=cursor.execute.call_args.args
        self.assertEqual(params,('employee:one',mark))
        self.assertIn("event='connection_check'",sql)
        self.assertIn("payload ->> 'channel'='mcp'",sql)
        self.assertIn("interval '15 minutes'",sql)
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertNotIn('OTHER',json.dumps(result))
        self.assertEqual(result['allowed_tools'],['gateway_access_profile'])

    def test_missing_database_is_unavailable(self):
        with patch.object(service,'postgres_enabled',return_value=False),self.assertRaises(RuntimeError):
            service.read_probe(actor(),service.new_mark())


class DiagnosticRouteTests(unittest.TestCase):
    def page(self,query='',current='default',result=None):
        mcp=FakeMCP();routes.register_connection_diagnostics_routes(mcp)
        request=Request({'type':'http','method':'GET','path':routes.PATH,'query_string':query.encode(),
                         'headers':[],'scheme':'https','server':('example.test',443)})
        with patch.object(routes.corporate_access,'enabled',return_value=True),\
             patch.object(routes,'web_actor',return_value=actor() if current=='default' else current),\
             patch.object(service,'read_probe',return_value=result) as read:
            response=asyncio.run(mcp.routes[routes.PATH](request))
        return response,read

    def test_first_page_cannot_claim_mcp_works(self):
        response,read=self.page()
        self.assertEqual(response.status_code,200)
        self.assertIn('пока не получен',response.body.decode())
        self.assertNotIn('Запрос дошёл через MCP',response.body.decode())
        self.assertEqual(response.headers['cache-control'],'no-store')
        read.assert_not_called()

    def test_not_authenticated_redirects_to_personal_login(self):
        response,read=self.page(current=None)
        self.assertIn(response.status_code,(302,303,307))
        read.assert_not_called()

    def test_missing_scope_denied(self):
        response,read=self.page(current=actor(scopes=()))
        self.assertEqual(response.status_code,403)
        read.assert_not_called()

    def test_subject_override_duplicate_and_bad_mark_are_rejected(self):
        for query in ['subject=other','check=x&check=y','check=%3Cscript%3E','check=BearerSECRET']:
            with self.subTest(query=query):
                response,read=self.page(query)
                self.assertEqual(response.status_code,400)
                self.assertNotIn('BearerSECRET',response.body.decode())
                read.assert_not_called()

    def test_result_lookup_uses_only_web_actor(self):
        mark=service.new_mark();response,read=self.page('check='+mark)
        self.assertEqual(response.status_code,200)
        self.assertEqual(read.call_args.args[0].subject,'employee:one')
        self.assertEqual(read.call_args.args[1],mark)

    def test_confirmed_result_shows_scopes_and_request_id(self):
        result={'checked_at':'2026-11-04T10:00:00+00:00','gateway_request_id':'gwc-'+'a'*32,
                'effective_scopes':['skills:read'],'allowed_tools':['gateway_access_profile'],
                'client':{'name':'opencode','version':'1.2.3'}}
        response,_=self.page('check='+service.new_mark(),result=result)
        text=response.body.decode()
        self.assertIn('Запрос дошёл через MCP',text)
        self.assertIn('skills:read',text)
        self.assertIn('opencode 1.2.3',text)
        self.assertIn('04.11.2026 13:00 МСК',text)
        self.assertIn('gwc-'+'a'*32,text)
        self.assertIn('Место выполнения чата сервер не определяет',text)

    def test_storage_error_is_redacted(self):
        mcp=FakeMCP();routes.register_connection_diagnostics_routes(mcp)
        request=Request({'type':'http','method':'GET','path':routes.PATH,
                         'query_string':('check='+service.new_mark()).encode(),'headers':[]})
        with patch.object(routes.corporate_access,'enabled',return_value=True),\
             patch.object(routes,'web_actor',return_value=actor()),\
             patch.object(service,'read_probe',side_effect=RuntimeError('postgres://PRIVATE')):
            response=asyncio.run(mcp.routes[routes.PATH](request))
        self.assertEqual(response.status_code,503)
        self.assertNotIn('PRIVATE',response.body.decode())


class DiagnosticToolTests(unittest.TestCase):
    def setUp(self):
        from gateway_mcp.tools import access_requests
        self.module=access_requests
        self.mcp=FakeMCP();access_requests.register_access_request_tools(self.mcp)
        self.run=MagicMock();self.run.arguments={};self.run.require_scope.return_value=actor()

    def test_legacy_profile_call_keeps_previous_result(self):
        with patch.object(self.module.ToolRun,'start',return_value=self.run),\
             patch.object(self.module,'access_profile',return_value={'actor':{'subject':'employee:one'}}),\
             patch.object(service,'record_probe') as record:
            result=json.loads(asyncio.run(self.mcp.tools['gateway_access_profile']()))
        self.assertEqual(result,{'ok':True,'actor':{'subject':'employee:one'}})
        record.assert_not_called()

    def test_valid_probe_adds_same_request_id_to_normal_audit(self):
        mark=service.new_mark();check={'diagnostic_id':mark,'gateway_request_id':'gwc-'+'a'*32,'recorded':True}
        with patch.object(self.module.ToolRun,'start',return_value=self.run),\
             patch.object(self.module,'access_profile',return_value={'actor':{}}),\
             patch.object(service,'record_probe',return_value=check):
            result=json.loads(asyncio.run(self.mcp.tools['gateway_access_profile'](diagnostic_id=mark)))
        self.assertEqual(result['connection_check'],check)
        self.assertEqual(self.run.arguments,{'diagnostic_id':mark,'gateway_request_id':check['gateway_request_id']})

    def test_invalid_probe_not_logged_or_sent_to_database(self):
        with patch.object(self.module.ToolRun,'start',return_value=self.run),\
             patch.object(self.module,'access_profile') as profile,\
             patch.object(service,'record_probe') as record,self.assertRaises(ValueError):
            asyncio.run(self.mcp.tools['gateway_access_profile'](diagnostic_id='Bearer PRIVATE'))
        profile.assert_not_called();record.assert_not_called()
        self.assertEqual(self.run.arguments,{})

    def test_probe_does_not_bypass_existing_scope_check(self):
        self.run.require_scope.side_effect=PermissionError('missing_scope')
        with patch.object(self.module.ToolRun,'start',return_value=self.run),\
             patch.object(self.module,'access_profile') as profile,\
             patch.object(service,'record_probe') as record,self.assertRaises(PermissionError):
            asyncio.run(self.mcp.tools['gateway_access_profile'](diagnostic_id=service.new_mark()))
        profile.assert_not_called();record.assert_not_called()
