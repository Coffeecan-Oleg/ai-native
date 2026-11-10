"""Public SDK delivery only; no real identity, token exchange or business API."""
import hashlib
import os
from unittest.mock import patch
import unittest
import test_bitrix_embed as fixtures

SDK_SHA='945ad491df8ffb7490b89f4914a439b5a3f575c950c3480562815716a3e07f23'

class FrameSdkDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture=self
        # Register patches on this running IsolatedAsyncioTestCase so its own
        # asyncio-aware cleanups run; an unstarted fixture would leak patches.
        fixtures.FrameHttpTests.setUp(self)

    def client(self):
        return fixtures.FrameHttpTests.client(self)

    async def test_sdk_is_exact_reviewed_official_asset_with_correct_type(self):
        with patch.object(fixtures.identity,'_request',side_effect=AssertionError('No outbound API')):
            async with self.fixture.client() as client:
                response=await client.get('/auth/bitrix24/frame-sdk.js')
        self.assertEqual(response.status_code,200)
        self.assertIn('application/javascript',response.headers['content-type'])
        self.assertEqual(response.headers['x-content-type-options'],'nosniff')
        self.assertEqual(hashlib.sha256(response.content).hexdigest(),SDK_SHA)
        self.assertNotIn('set-cookie',response.headers)

    async def test_browser_loader_has_no_external_sdk_dependency(self):
        async with self.fixture.client() as client:
            response=await client.get('/auth/bitrix24/frame-client.js')
        self.assertIn("sdk.src = '/auth/bitrix24/frame-sdk.js'",response.text)
        self.assertNotIn('https://api.bitrix24',response.text)
        self.assertNotIn('getAuth',response.text)
        self.assertNotIn('callMethod',response.text)

    async def test_successful_launch_tightens_script_policy_and_retains_session_limits(self):
        with patch.object(fixtures.embed,'complete_launch',return_value=self.fixture.employee):
            async with self.fixture.client() as client:
                response=await client.post('/auth/bitrix24/callback',params=fixtures.QUERY,
                    data=fixtures.FIELDS,headers={'Origin':fixtures.embed.PORTAL_ORIGIN})
        self.assertEqual(response.status_code,200)
        self.assertIn("script-src 'self';",response.headers['content-security-policy'])
        self.assertNotIn('api.bitrix24',response.headers['content-security-policy'])
        self.assertIn('frame-ancestors '+fixtures.embed.PORTAL_ORIGIN,response.headers['content-security-policy'])
        for flag in ['HttpOnly','Secure','SameSite=None','Partitioned']:
            self.assertIn(flag,response.headers['set-cookie'])

    async def test_sdk_route_is_feature_gated_and_cannot_proxy_query_urls(self):
        with patch.object(fixtures.identity,'_request',side_effect=AssertionError('No proxy fetch')):
            async with self.fixture.client() as client:
                response=await client.get('/auth/bitrix24/frame-sdk.js?url=https://foreign.example.invalid/private')
                self.assertEqual(hashlib.sha256(response.content).hexdigest(),SDK_SHA)
                with patch.dict(os.environ,{'GATEWAY_BITRIX_EMBED_ENABLED':'false'}):
                    self.assertEqual((await client.get('/auth/bitrix24/frame-sdk.js')).status_code,404)

if __name__=='__main__':unittest.main()
