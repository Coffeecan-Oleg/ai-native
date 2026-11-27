"""Transport, data minimization and negative tests; synthetic credentials only."""
import copy
import io
import json
import logging
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from gateway_mcp.backends import call_backend
from gateway_mcp.backends import unisender_readonly as backend
from gateway_mcp.backends.common import BackendConfigError, BackendRouteError
from gateway_mcp.services import unisender_readonly as boundary
from gateway_mcp.services import auth  # Load SDK types before mocking its HTTP client.

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = json.loads((ROOT/'gateway-platforma-tools.json').read_text(encoding='utf-8'))['tools']
ROUTES = {r['name']:r for r in REGISTRY if r['backend']=='unisender'}
KEY = 'synthetic-not-a-real-key-20261026'
REAL_CLIENT = httpx.AsyncClient


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.payload = {'result':[{'id':19445573,'title':'Synthetic list','email':'not-returned@example.invalid'}]}
        self.status = 200
        async def handle(request):
            self.requests.append(request)
            return httpx.Response(self.status,json=self.payload,headers={'Location':'https://forbidden.invalid/'})
        def factory(**kwargs):
            self.assertFalse(kwargs['follow_redirects']);self.assertFalse(kwargs['trust_env'])
            return REAL_CLIENT(transport=httpx.MockTransport(handle),**kwargs)
        self.enterContext(patch.object(backend,'_credential',return_value=KEY))
        self.enterContext(patch.object(boundary,'require_actor'))
        self.enterContext(patch.object(httpx,'AsyncClient',side_effect=factory))

    async def call(self,name='unisender.getLists',arguments=None,route=None):
        return await call_backend(route or ROUTES[name],arguments or {})

    async def test_lists_use_post_body_and_whitelisted_result_fields(self):
        result=await self.call();data=result['data']
        request=self.requests[0]
        self.assertEqual(request.method,'POST');self.assertEqual(request.url.host,'api.unisender.com')
        self.assertEqual(str(request.url),'https://api.unisender.com/ru/api/getLists')
        self.assertIn(KEY,request.content.decode());self.assertNotIn(KEY,str(request.url))
        self.assertEqual(data['lists'],[{'id':19445573,'title':'Synthetic list'}])
        self.assertFalse(data['synthetic']);self.assertTrue(data['upstream_contacted'])
        self.assertNotIn(KEY,json.dumps(result));self.assertNotIn('not-returned@',json.dumps(result))

    async def test_count_is_typed_and_contains_no_contacts(self):
        self.payload={'result':{'listId':19445573,'searchParams':{'type':'address'},'count':'125','contacts':['not-returned@example.invalid']}}
        data=(await self.call('unisender.getContactCount',{'list_id':19445573}))['data']
        self.assertEqual((data['count'],data['contact_type']),(125,'address'))
        self.assertIn('params%5Btype%5D=address',self.requests[0].content.decode())
        self.assertNotIn('not-returned@',json.dumps(data))

    async def test_all_other_methods_denied_before_network(self):
        for method in ('sendEmail','sendSms','createCampaign','createList','subscribe','exportContacts','getContact','getFields'):
            with self.subTest(method=method),self.assertRaises(PermissionError):
                await self.call(route=ROUTES['unisender.getLists']|{'name':'unisender.'+method})
        self.assertEqual(self.requests,[])

    async def test_arbitrary_inputs_cannot_override_request(self):
        for key in ('method','url','api_key','body','headers','params','email','search'):
            with self.subTest(key=key),self.assertRaises(BackendRouteError):
                await self.call(arguments={key:'forbidden'})
            with self.subTest(count_key=key),self.assertRaises(BackendRouteError):
                await self.call('unisender.getContactCount',{'list_id':19445573,key:'forbidden'})
        self.assertEqual(self.requests,[])

    async def test_invalid_identifiers_and_types_denied(self):
        for value in (True,0,-1,'19445573',None,[],2**63):
            with self.subTest(value=value),self.assertRaises(BackendRouteError):
                await self.call('unisender.getContactCount',{'list_id':value})
        with self.assertRaises(BackendRouteError):
            await self.call('unisender.getContactCount',{'list_id':19445573,'contact_type':'all'})
        self.assertEqual(self.requests,[])

    async def test_route_metadata_cannot_enable_write_or_different_scope(self):
        for change in ({'scope':'platforma:read'},{'http_method':'POST'},{'backend':'other'}):
            with self.subTest(change=change),self.assertRaises(PermissionError):
                await self.call(route=ROUTES['unisender.getLists']|change)
        self.assertEqual(self.requests,[])

    async def test_upstream_error_and_logs_do_not_echo_payload_or_key(self):
        self.payload={'error':KEY+' not-returned@example.invalid','code':KEY,'result':'unsafe'}
        log=io.StringIO();handler=logging.StreamHandler(log);root=logging.getLogger()
        root.addHandler(handler)
        try:
            with self.assertRaisesRegex(BackendRouteError,'UNISENDER_UPSTREAM_REJECTED') as cm:
                await self.call()
        finally:root.removeHandler(handler)
        self.assertNotIn(KEY,str(cm.exception)+log.getvalue())
        self.assertNotIn('not-returned@',str(cm.exception)+log.getvalue())

    async def test_redirects_and_http_failures_do_not_follow(self):
        self.status=302
        with self.assertRaisesRegex(BackendRouteError,'UNISENDER_UPSTREAM_HTTP_ERROR'):await self.call()
        self.assertEqual(len(self.requests),1)

    async def test_malformed_count_or_wrong_list_cannot_be_reported(self):
        for result in ({'listId':9,'searchParams':{'type':'address'},'count':'1'},
                       {'listId':19445573,'searchParams':{'type':'address'},'count':True},
                       {'listId':19445573,'searchParams':{'type':'phone'},'count':'1'}):
            self.payload={'result':result}
            with self.subTest(result=result),self.assertRaises(BackendRouteError):
                await self.call('unisender.getContactCount',{'list_id':19445573})

    async def test_revoked_or_unapproved_actor_causes_no_network(self):
        with patch.object(boundary,'require_actor',side_effect=PermissionError('inactive')):
            with self.assertRaises(PermissionError):await self.call()
        self.assertEqual(self.requests,[])

    async def test_key_echo_in_title_is_rejected(self):
        self.payload={'result':[{'id':19445573,'title':KEY}]}
        with self.assertRaises(BackendRouteError):await self.call()

    async def test_oversized_response_is_rejected(self):
        self.payload={'result':'x'*(backend.MAX_RESPONSE+1)}
        with self.assertRaisesRegex(BackendRouteError,'UNISENDER_RESPONSE_TOO_LARGE'):await self.call()


class SecretAndMethodTests(unittest.IsolatedAsyncioTestCase):
    async def test_low_level_methods_cannot_become_an_api_proxy(self):
        with patch.object(backend,'_credential') as read,patch.object(httpx,'AsyncClient') as client:
            for method in ('sendEmail','getContact','exportContacts','../getLists'):
                with self.assertRaises(PermissionError):await backend._post_api(method,{})
            read.assert_not_called();client.assert_not_called()

    async def test_missing_or_wrong_service_credential_fails_closed(self):
        for value in (None,{'access_token':KEY,'scopes':['sendEmail']}, {'access_token':'bad\nkey','scopes':list(boundary.METHODS)}):
            with patch('gateway_mcp.services.storage.get_user_oauth_token',return_value=value):
                with self.assertRaisesRegex(BackendConfigError,'UNISENDER_CREDENTIAL_UNAVAILABLE'):backend._credential()


if __name__=='__main__':unittest.main()
