"""Bounded personal MCP calls against disposable PostgreSQL, never UniSender."""
import json
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
import test_reader_onboarding as onboarding_test
from gateway_mcp.services import auth, platforma_onboarding as service
from gateway_mcp.backends.unisender_demo import CAMPAIGN_ID, ROUTE_NAME


class UniSenderDemoMcpTests(onboarding_test.ReaderFixture):
    rpc=onboarding_test.ProtocolTests.rpc
    tool=onboarding_test.ProtocolTests.tool

    def setUp(self):
        super().setUp()
        from gateway_mcp.server import create_mcp
        self.http=self.enterContext(TestClient(create_mcp().streamable_http_app(),base_url='https://gateway.example.invalid'))
        self.row=self.approve()
        actor=service.oauth_reader_actor(self.fresh())
        self.headers={'Authorization':'Bearer '+auth.mint_gateway_token(actor),'Accept':'application/json, text/event-stream'}
        response=self.rpc({'id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'isolated-demo-client','version':'1'}}})
        self.headers['MCP-Protocol-Version']=response['result']['protocolVersion']
        self.rpc({'method':'notifications/initialized'});self.seq=10

    def read(self,identifier=CAMPAIGN_ID):
        return self.tool('gateway_call_tool',{'tool_name':ROUTE_NAME,'arguments_json':json.dumps({'campaign_id':identifier})})

    def test_approved_reader_discovers_and_reads_audited_fictional_aggregate(self):
        result,found=self.tool('gateway_search_tools',{'query':'unisender'})
        self.assertFalse(result.get('isError'),found)
        self.assertEqual([r['name'] for r in found['tools']],[ROUTE_NAME])
        with patch('httpx.AsyncClient',side_effect=AssertionError('No external calls allowed')):
            result,data=self.read()
        self.assertFalse(result.get('isError'),data);self.assertTrue(data['data']['synthetic'])
        self.assertFalse(data['data']['upstream_contacted'])
        self.assertEqual(data['data']['statistics']['delivered'],98)
        from gateway_mcp.services.storage_audit import list_audit_events
        events=list_audit_events(actor_subject=self.reader.subject,event='resource_access',
            tool=ROUTE_NAME,gateway_request_id=data['gateway_request_id'])
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['decision'],'allow')

    def test_different_campaign_and_real_unisender_routes_are_denied(self):
        self.assertTrue(self.read('42')[0].get('isError'))
        self.assertTrue(self.read('LAB-UNISENDER-002')[0].get('isError'))
        for route in ('unisender.campaign.stats','unisender_list_campaigns','unisender.send','platforma.unisender-demo.send'):
            result,_=self.tool('gateway_call_tool',{'tool_name':route,'arguments_json':'{}'})
            self.assertTrue(result.get('isError'),route)

    def test_owner_uses_same_bounded_read_route(self):
        actor=service.oauth_reader_actor(self.owner)
        self.headers['Authorization']='Bearer '+auth.mint_gateway_token(actor)
        result,data=self.read()
        self.assertFalse(result.get('isError'),data)
        self.assertTrue(data['data']['synthetic'])

    def test_revocation_blocks_the_old_token_and_keeps_owner_statistics_private(self):
        self.assertFalse(self.read()[0].get('isError'))
        service.decide(self.owner,self.row['id'],'revoked',self.row['revision'],'End fictional demo')
        self.assertTrue(self.read()[0].get('isError'))
        self.assertEqual(self.rpc({'id':200,'method':'tools/list'})['result']['tools'],[])

    def test_missing_grant_and_expiry_block_already_issued_token(self):
        from gateway_mcp.services.storage_core import _connect
        with _connect() as conn:
            conn.execute('update access_scope_grants set revoked_at=now() where id=%s',(self.row['scope_grant_ids'][0],))
        self.assertTrue(self.read()[0].get('isError'))
        with _connect() as conn:
            conn.execute('update access_scope_grants set revoked_at=null where id=%s',(self.row['scope_grant_ids'][0],))
            conn.execute("update platforma_reader_requests set approved_until=now()-interval '1 second' where id=%s",(self.row['id'],))
        self.assertTrue(self.read()[0].get('isError'))


if __name__=='__main__':unittest.main()
