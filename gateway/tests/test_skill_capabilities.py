"""No upstream calls: declarations are bounded data, never executable instructions."""
import hashlib,json,tempfile,unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from gateway_mcp.services import skill_capabilities as cap,skill_activity as activity,corporate_skills,addon_guard,corporate_access,telemetry
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.routes.skill_activity import capability_panel

class CapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.actor=GatewayActor(subject='synthetic',scopes=('skills:read','tools:call','unisender:read'))
        self.row={'name':'shared-example','title':'Example','version':'1.0.0','files':{'SKILL.md':{'sha256':'a'*64}}}
        self.ops=[{'id':'draft','title':'Draft text','kind':'assistant'},{'id':'lists','title':'Read lists','kind':'route','route':'unisender.getLists'},
                  {'id':'send','title':'Send letters','kind':'unavailable'}]
        self.tmp=Path(self.enterContext(tempfile.TemporaryDirectory()));self.registry=self.tmp/'registry.json'
        self.registry.write_text(json.dumps({'schema':1,'skills':{'shared-example':{'manifest_sha256':cap.manifest_hash(self.row),'operations':self.ops}}}))
        self.enterContext(patch.object(cap,'REGISTRY',self.registry))
        self.enterContext(patch.object(corporate_skills,'list_skills',return_value={'skills':[self.row]}))
        self.enterContext(patch.object(cap,'read_json',return_value={'tools':[{'name':'unisender.getLists','scope':'unisender:read'}]}))
        self.guard=self.enterContext(patch.object(addon_guard,'check_call',return_value={}))

    def test_known_exact_revision_reports_partial_read_only_not_external_health(self):
        r=cap.check(self.actor,'shared-example')
        self.assertEqual([x['status'] for x in r['operations']],['assistant_task','route_allowed','not_connected'])
        self.assertEqual(r['requirements_source'],'reviewed_registry');self.assertFalse(r['external_service_checked'])
        self.guard.assert_called_once_with(self.actor,'gateway_call_tool',{'tool_name':'unisender.getLists'})

    def test_missing_scope_and_tool_ceiling_are_distinct(self):
        self.assertEqual(cap.check(replace(self.actor,scopes=('skills:read',)),'Example')['operations'][1]['status'],'missing_scope')
        actor=replace(self.actor,tool_allowlist=('gateway_call_tool',))
        self.assertEqual(cap.check(actor,'shared-example')['operations'][1]['status'],'token_ceiling')
        self.guard.assert_not_called()

    def test_configured_but_denied_route_is_not_green(self):
        self.guard.side_effect=PermissionError('secret-like internal details')
        r=cap.check(self.actor,'shared-example')
        self.assertEqual(r['operations'][1]['status'],'access_denied');self.assertNotIn('secret-like',json.dumps(r))

    def test_unknown_and_stale_declarations_are_not_ready(self):
        self.row['files']['SKILL.md']['sha256']='b'*64
        self.assertEqual(cap.check(self.actor,'shared-example')['requirements_source'],'registry_stale')
        self.row['name']='other'
        self.assertEqual(cap.check(self.actor,'other')['requirements_source'],'requirements_missing')
        self.guard.assert_not_called()

    def test_declared_requirements_verify_resource_hash_and_mark_author(self):
        self.row['files']['requirements.json']={'sha256':'c'*64,'bytes':100}
        with patch.object(corporate_skills,'read_skill',return_value={'sha256':'c'*64,'content':json.dumps({'schema':1,'operations':self.ops})}):
            self.assertEqual(cap.check(self.actor,'shared-example')['requirements_source'],'author_declaration')
        with patch.object(corporate_skills,'read_skill',return_value={'sha256':'d'*64,'content':'{}'}):
            self.assertEqual(cap.check(self.actor,'shared-example')['requirements_source'],'requirements_invalid')

    def test_malformed_oversized_and_executable_declarations_are_rejected(self):
        valid={'schema':1,'operations':self.ops}
        for data in [[],{'schema':True,'operations':[]},{**valid,'command':'run'},{'schema':1,'operations':self.ops*4},
                     {'schema':1,'operations':[{'id':'x','title':'Bad','kind':'shell','route':'x'}]},
                     {'schema':1,'operations':[{'id':'x','title':'Bad\ntext','kind':'assistant'}]},
                     {'schema':1,'operations':[self.ops[0],self.ops[0]]}]:
            with self.subTest(data=data),self.assertRaises(ValueError):cap.validate_operations(data)
        self.row['files']['requirements.json']={'bytes':8193,'sha256':'b'*64}
        self.assertEqual(cap.check(self.actor,'shared-example')['requirements_source'],'requirements_invalid')

    def test_private_unknown_or_missing_scope_never_leaks_row(self):
        for actor,name in [(self.actor,'private'),(replace(self.actor,scopes=()),'shared-example')]:
            with self.assertRaises(PermissionError):cap.check(actor,name)

    def test_registry_hash_includes_all_resources_and_is_order_independent(self):
        original=cap.manifest_hash(self.row)
        self.row['files']['references/help.md']={'sha256':'b'*64}
        self.assertNotEqual(cap.manifest_hash(self.row),original)
        self.assertEqual(cap.manifest_hash(self.row),cap.manifest_hash({**self.row,'files':dict(reversed(list(self.row['files'].items())))}))

    def test_browser_escapes_untrusted_titles_and_states_limits(self):
        self.ops[0]['title']='<script>alert(1)</script>'
        self.registry.write_text(json.dumps({'schema':1,'skills':{'shared-example':{'manifest_sha256':cap.manifest_hash(self.row),'operations':self.ops}}}))
        html=capability_panel(cap.check(self.actor,'Example'))
        self.assertIn('&lt;script&gt;',html);self.assertNotIn('<script>',html);self.assertIn('внешние API',html)

class ActivityBoundaryTests(unittest.TestCase):
    def test_failed_audit_storage_does_not_forge_a_receipt(self):
        actor=GatewayActor(subject='own');result={'skill':'shared-example','path':'SKILL.md','encoding':'utf-8','skill_version':'1','sha256':'a'*64,'manifest_sha256':'b'*64,'skill_pack':'x'}
        with patch.object(activity,'enabled',return_value=True),patch.object(activity,'append_audit_event',side_effect=RuntimeError('private')):
            r=activity.record_read(actor,result)
        self.assertFalse(r['recorded']);self.assertIsNone(r['id']);self.assertFalse(r['execution_confirmed'])

    def test_binary_nonroot_and_disabled_reads_have_no_receipt(self):
        with patch.object(activity,'enabled',return_value=False):self.assertIsNone(activity.record_read(None,{}))
        with patch.object(activity,'enabled',return_value=True),patch.object(activity,'append_audit_event') as insert:
            self.assertIsNone(activity.record_read(None,{'path':'references/x.md'}));insert.assert_not_called()

    def test_client_flags_are_rejected_and_hashes_are_server_derived(self):
        proof={'skill_read_id':'sr-'+'a'*32,'sha256':'b'*64,'manifest_sha256':'c'*64}
        with patch.object(activity,'receipt',return_value=proof):
            self.assertEqual(activity.link_metadata(None,'s','p','v',{'skill_read_id':proof['skill_read_id'],'read_sha256':'fake'})['read_sha256'],'b'*64)
            for data in [{'execution_confirmed':True},{'read_sha256':'a'*64},{'skill_read_id':proof['skill_read_id'],'prompt':'private'}]:
                with self.assertRaises(PermissionError):activity.link_metadata(None,'s','p','v',data)

    def test_replay_reports_the_persisted_version_not_new_claimed_link(self):
        actor=GatewayActor(subject='own')
        with patch.object(activity,'enabled',return_value=False),patch.object(telemetry,'insert_assistant_skill_event',return_value={'inserted':False,'skill_id':'old','skill_pack':'p','skill_version':'1','metadata':{}}):
            r=telemetry.record_skill_event(actor=actor,event_type='started',agent='codex',skill_id='new',skill_version='2',metadata_json='{"skill_read_id":"fake"}')
        self.assertTrue(r['duplicate']);self.assertFalse(r['event']['read_link_verified']);self.assertEqual(r['event']['skill_version'],'1')

    def test_employee_activity_exact_path_does_not_open_arbitrary_admin(self):
        actor=GatewayActor(subject='employee')
        with patch.object(corporate_access,'fresh_actor',return_value=actor),patch.object(corporate_access,'is_owner',return_value=False):
            self.assertTrue(corporate_access.web_path_allowed(actor,'/platforma/skills/activity'))
            self.assertFalse(corporate_access.web_path_allowed(actor,'/admin/users'))
            self.assertFalse(corporate_access.web_path_allowed(actor,'/platforma/skills/activity/foreign'))
