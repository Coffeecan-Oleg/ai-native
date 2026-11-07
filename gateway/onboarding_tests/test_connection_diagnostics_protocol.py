"""Real MCP+ASGI+disposable PG; identity boundaries use fictional actors, not Bitrix."""
import json
import os
import re
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from gateway_mcp.routes import connection_diagnostics as routes
from gateway_mcp.services import connection_diagnostics as service
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.tools.access_requests import register_access_request_tools


@unittest.skipUnless(os.getenv('ONBOARDING_TEST_DATABASE_URL'),'Requires new disposable loopback PostgreSQL')
class ConnectionDiagnosticsProtocolTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,{'GATEWAY_DATABASE_URL':os.environ['ONBOARDING_TEST_DATABASE_URL'],
             'GATEWAY_AUTH_ENABLED':'true','GATEWAY_PUBLIC_URL':'https://gateway.example.invalid',
             'GATEWAY_BRAND':'akmetron'}))
        unique=uuid4().hex
        self.actors={name:GatewayActor(subject='diagnostic:'+unique+':'+name,login=name,
                      scopes=('skills:read','tools:read','knowledge:read') if name!='limited' else ())
                     for name in ['alice','bob','limited']}
        actors=self.actors
        class Verifier:
            async def verify_token(self,token):
                if token not in actors:return None
                current=actors[token]
                return AccessToken(token=token,client_id=current.subject,scopes=list(current.scopes),
                                   expires_at=int(time.time())+600)
        self.mcp=FastMCP('diagnostic-copy',token_verifier=Verifier(),json_response=True,
             transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                  allowed_hosts=['gateway.example.invalid'],allowed_origins=['https://gateway.example.invalid']),
             auth=AuthSettings(issuer_url='https://gateway.example.invalid',
                               resource_server_url='https://gateway.example.invalid/mcp',required_scopes=[]))
        self.enterContext(patch.object(routes.corporate_access,'enabled',return_value=True))
        def current_actor():
            token=get_access_token()
            return actors.get(token.token,GatewayActor(subject='anonymous')) if token else GatewayActor(subject='anonymous')
        self.enterContext(patch('gateway_mcp.services.auth.current_actor',side_effect=current_actor))
        self.enterContext(patch('gateway_mcp.audit.current_actor',side_effect=current_actor))
        self.enterContext(patch.object(routes,'web_actor',side_effect=lambda r:actors.get(r.headers.get('x-fictional-web-user'))))
        routes.register_connection_diagnostics_routes(self.mcp)
        register_access_request_tools(self.mcp)
        self.http=self.enterContext(TestClient(self.mcp.streamable_http_app(),base_url='https://gateway.example.invalid'))
        self.start('alice')

    def start(self,name):
        self.headers={'Authorization':'Bearer '+name,'Accept':'application/json, text/event-stream'}
        reply=self.http.post('/mcp',headers=self.headers,json={'jsonrpc':'2.0','id':1,'method':'initialize',
           'params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'codex-desktop','version':'9.9.9'}}})
        self.assertEqual(reply.status_code,200,reply.text)
        self.headers['Mcp-Session-Id']=reply.headers['Mcp-Session-Id']
        self.headers['MCP-Protocol-Version']=reply.json()['result']['protocolVersion']
        self.http.post('/mcp',headers=self.headers,json={'jsonrpc':'2.0','method':'notifications/initialized'})
        self.seq=10

    def call(self,arguments):
        self.seq+=1
        reply=self.http.post('/mcp',headers=self.headers,json={'jsonrpc':'2.0','id':self.seq,
            'method':'tools/call','params':{'name':'gateway_access_profile','arguments':arguments}})
        self.assertEqual(reply.status_code,200,reply.text)
        result=reply.json()['result']
        return result,json.loads(result['content'][0]['text']) if not result.get('isError') else None

    def page(self,name='alice',query=''):
        return self.http.get(routes.PATH+query,headers={'x-fictional-web-user':name})

    def test_full_browser_mark_mcp_call_and_persistent_result(self):
        initial=self.page();self.assertEqual(initial.status_code,200)
        mark=re.search(r'diag-[0-9]{10}-[0-9a-f]{24}',initial.text).group(0)
        self.assertIn('пока не получен',self.page(query='?check='+mark).text)
        result,data=self.call({'diagnostic_id':mark})
        self.assertFalse(result.get('isError'),result)
        self.assertTrue(data['connection_check']['recorded'])
        page=self.page(query='?check='+mark)
        self.assertIn('Запрос дошёл через MCP',page.text)
        self.assertIn('codex 9.9.9',page.text)
        self.assertIn(data['connection_check']['gateway_request_id'],page.text)
        self.assertNotIn('bob',page.text)

    def test_same_mark_from_another_actor_does_not_confirm_my_page(self):
        mark=service.new_mark();self.start('bob')
        result,data=self.call({'diagnostic_id':mark});self.assertFalse(result.get('isError'),result)
        self.assertIsNone(service.read_probe(self.actors['alice'],mark))
        self.assertIn('пока не получен',self.page(query='?check='+mark).text)
        self.assertNotIn(data['connection_check']['gateway_request_id'],self.page(query='?check='+mark).text)
        self.assertIn('Запрос дошёл через MCP',self.page(name='bob',query='?check='+mark).text)

    def test_existing_profile_without_mark_still_works(self):
        result,data=self.call({})
        self.assertFalse(result.get('isError'),result)
        self.assertNotIn('connection_check',data)
        self.assertEqual(data['actor']['subject'],self.actors['alice'].subject)

    def test_invalid_mark_is_not_written_into_audit(self):
        result,_=self.call({'diagnostic_id':'Bearer PRIVATE-DIAGNOSTIC-SENTINEL'})
        self.assertTrue(result.get('isError'))
        from gateway_mcp.services.storage_core import _connect
        with _connect() as conn:
            row=conn.execute('select count(*) as n from audit_events where actor_subject=%s and payload::text like %s',
               (self.actors['alice'].subject,'%PRIVATE-DIAGNOSTIC-SENTINEL%')).fetchone()
        self.assertEqual(row['n'],0)

    def test_missing_permission_and_unauthenticated_request_are_not_confirmed(self):
        mark=service.new_mark();self.start('limited')
        result,_=self.call({'diagnostic_id':mark});self.assertTrue(result.get('isError'))
        self.assertIsNone(service.read_probe(self.actors['limited'],mark))
        reply=self.http.post('/mcp',headers={'Accept':'application/json, text/event-stream'},
            json={'jsonrpc':'2.0','id':100,'method':'tools/list'})
        self.assertEqual(reply.status_code,401)
