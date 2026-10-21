"""Isolated MCP fixtures; never use live identities, grants or databases."""
import base64
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import addon_tests.test_addon_http as fixtures
from gateway_mcp.services import corporate_skills as skills, branding, auth
from gateway_mcp.services.policy import GatewayActor


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'GATEWAY_CORPORATE_SKILLS_ENABLED': 'true'}))

    def test_exact_catalog_and_text(self):
        result = skills.list_skills()
        self.assertEqual(len(result['skills']), 5)
        for item in result['skills']:
            doc = skills.read_skill(item['name'])
            self.assertEqual(doc['encoding'], 'utf-8')
            self.assertIn('name: '+item['name'], doc['content'])
            self.assertIn('не даёт права отправлять сообщения', doc['content'])

    def test_binary_chunks_reassemble_exact_logo(self):
        blob = bytearray(); offset = 0
        while True:
            item = skills.read_skill('akmetron-design-system', 'assets/logo-mark.png', offset, 4096)
            self.assertEqual(item['offset'], offset)
            blob.extend(base64.b64decode(item['content'], validate=True))
            if item['next_offset'] is None:break
            offset = item['next_offset']
        self.assertEqual(len(blob), item['total_bytes'])
        self.assertEqual(hashlib.sha256(blob).hexdigest(), item['sha256'])

    def test_no_arbitrary_path_or_skill(self):
        for value in ['../SKILL.md', '/etc/passwd', 'assets/../../SKILL.md', 'C:/secret', 'assets\\logo-mark.png', 'assets//logo-mark.png', '.env']:
            with self.subTest(value=value), self.assertRaises(PermissionError):
                skills.read_skill('akmetron-design-system', value)
        with self.assertRaises(PermissionError):skills.read_skill('another-skill')

    def test_bounded_chunks(self):
        for offset,limit in [(-1,4),(0,0),(0,24577),(True,4),(999999,4)]:
            with self.subTest(offset=offset,limit=limit), self.assertRaises(ValueError):
                skills.read_skill('akmetron-design-system','assets/logo-mark.png',offset,limit)

    def test_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); folder=root/'test-skill';folder.mkdir()
            (folder/'SKILL.md').write_text('changed')
            (root/'catalog.json').write_text(json.dumps({'schema':1,'pack':'akmetron-corporate','skills':[{'name':'test-skill','files':{'SKILL.md':{'bytes':7,'sha256':'0'*64}}}]}))
            with patch.object(skills,'ROOT',root), self.assertRaises(ValueError):skills.read_skill('test-skill')

    def test_disabled_pack_fails_closed(self):
        with patch.dict(os.environ,{'GATEWAY_CORPORATE_SKILLS_ENABLED':'false'}), self.assertRaises(PermissionError):skills.list_skills()


class CorporateMcpTests(fixtures.ProtocolTests):
    def rpc(self, body):
        self.headers['Host']='127.0.0.1:8000'
        return super().rpc(body)

    def save(self):
        owner=self.profiles['profiles']['test:owner']
        known={r['id'] for r in owner['resources']}
        for name in ['gateway_corporate_skill_list','gateway_corporate_skill_read']:
            if 'mcp:'+name not in known:owner['resources'].append({'id':'mcp:'+name,'kind':'tool','operations':['read']})
        super().save()

    def setUp(self):
        self.enterContext(patch.dict(os.environ,{'GATEWAY_BRAND':'akmetron','GATEWAY_CORPORATE_SKILLS_ENABLED':'true','GATEWAY_PLATFORMA_ONBOARDING_ENABLED':'false'}))
        super().setUp()

    def test_corporate_reads_through_actual_mcp(self):
        self.assertEqual(len(self.tool('gateway_corporate_skill_list')[1]['skills']),5)
        self.assertIn('Дизайн-система',self.tool('gateway_corporate_skill_read',{'skill':'akmetron-design-system'})[1]['content'])
        self.assertTrue(self.tool('gateway_corporate_skill_read',{'skill':'akmetron-design-system','path':'../.env'})[0].get('isError'))

    def test_profile_revocation_blocks_corporate_reads(self):
        self.profiles['profiles']['test:owner']['status']='revoked';self.save()
        self.assertTrue(self.tool('gateway_corporate_skill_list')[0].get('isError'))

    def test_existing_finite_token_does_not_gain_new_tools(self):
        actor=GatewayActor(subject='test:owner',scopes=('skills:read',),tool_allowlist=('gateway_access_profile',))
        self.headers['Authorization']='Bearer '+auth.mint_gateway_token(actor)
        self.headers.pop('Mcp-Session-Id',None)
        self.rpc({'id':900,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'old-finite-credential','version':'1'}}})
        self.rpc({'method':'notifications/initialized'})
        self.assertTrue(self.tool('gateway_corporate_skill_list')[0].get('isError'))

    def test_local_assets_and_no_private_static_files(self):
        response=self.http.get('/branding/akmetron.css')
        self.assertEqual(response.status_code,200)
        self.assertIn('--brand-blue:      #004180',response.text)
        self.assertEqual(self.http.get('/branding/logo-mark.png').status_code,200)
        self.assertEqual(self.http.get('/branding/fonts/inter-cyr-400.woff2').status_code,200)
        for path in ['/branding/.env','/branding/catalog.json','/branding/%2e%2e/policy.json']:
            self.assertIn(self.http.get(path).status_code,[404,400])
        with patch.dict(os.environ,{'GATEWAY_BRAND':''}):
            self.assertEqual(self.http.get('/branding/logo-mark.png').status_code,404)

    def test_panel_shell_escapes_and_brand_is_optional(self):
        from gateway_mcp.routes.admin_ui import admin_shell
        html=admin_shell(title='<unsafe>',active='platforma-readers',actor=self.actor,body='<p>Fixture</p>')
        self.assertIn('&lt;unsafe&gt;',html);self.assertIn('Акметрон',html)
        with patch.dict(os.environ,{'GATEWAY_BRAND':''}):
            self.assertNotIn('/branding/akmetron.css',admin_shell(title='Fixture',active='',actor=self.actor,body=''))

if __name__=='__main__':unittest.main()
