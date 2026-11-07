from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from gateway_mcp.services import addon_guard
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.tools.telemetry import register_telemetry_tools
from test_platforma_demo import run_read

ROOT=Path(__file__).resolve().parents[1]
OWNER=GatewayActor(subject='yandex:9001',scopes=('telemetry:read','telemetry:write','platforma:write'))
REGISTRY=json.loads((ROOT/'gateway-platforma-tools.json').read_text(encoding='utf-8'))


class TelemetryBoundaryTests(unittest.TestCase):
    def setUp(self):
        names=addon_guard.READ_TOOLS | addon_guard.WRITE_TOOLS
        self.profile={'subject':OWNER.subject,'project':'APP11','status':'active',
           'resources':[{'id':'mcp:'+n,'kind':'tool','operations':['read' if n in addon_guard.READ_TOOLS else 'write']} for n in names]
              +[{'id':n,'kind':'backend_route','operations':['write' if n in addon_guard.WRITE_ROUTES else 'read']} for n in addon_guard.ROUTES | set(addon_guard.WRITE_ROUTES)],
           'skill_telemetry':{'project':'platforma-training','agents':['codex'],
              'scopes':['platforma:read','platforma:write','telemetry:write'],
              'skills':{'platforma-training-service-reader':{'pack':'platforma-training','version':'0.1.0'}}}}
        self.patch=patch.object(addon_guard,'profile_for',return_value=self.profile);self.patch.start()
        self.env=patch.dict(os.environ,{'GATEWAY_PLATFORMA_OWNER_SUBJECT':OWNER.subject,
                'GATEWAY_TOOLS_FILE':str(ROOT/'gateway-platforma-tools.json')});self.env.start()
        self.args={'agent':'codex','skill_id':'platforma-training-service-reader',
                   'skill_pack':'platforma-training','skill_version':'0.1.0','project':'platforma-training',
                   'session_id':'sandbox-test-session','client':'codex-desktop'}

    def tearDown(self):self.patch.stop();self.env.stop()

    def check(self,name='gateway_telemetry_skill_started',**overrides):
        return addon_guard.check_call(OWNER,name,{**self.args,**overrides})

    def test_three_minimal_event_types_are_allowed(self):
        for name in ['gateway_telemetry_skill_started','gateway_telemetry_skill_completed','gateway_telemetry_skill_failed']:
            self.assertIs(self.check(name),self.profile)

    def test_content_metadata_is_rejected_before_recording(self):
        for key in ['prompt','request','arbitrary']:
            with self.subTest(key=key),self.assertRaises(PermissionError):
                self.check(metadata_json=json.dumps({key:'DO-NOT-STORE-CONTENT'}))

    def test_unapproved_identity_fields_are_rejected(self):
        for change in [{'skill_id':'unknown'},{'skill_pack':'foreign'},{'skill_version':'999'},
                       {'agent':'unknown'},{'project':'foreign'},{'client':'private-content'},
                       {'session_id':'raw text with spaces'},{'duration_ms':-1},
                       {'duration_ms':True},{'duration_ms':86400001},
                       {'mcp_routes_json':'["mail.send"]'},{'missing_scopes_json':'["access:admin"]'}]:
            with self.subTest(change=change),self.assertRaises(PermissionError):self.check(**change)

    def test_reader_profile_cannot_emit_events(self):
        reader=GatewayActor(subject='yandex:9002',scopes=('platforma:read',))
        self.profile['subject']=reader.subject
        self.profile['resources']=[r for r in self.profile['resources'] if r['id']!='mcp:gateway_telemetry_skill_started']
        with self.assertRaises(PermissionError):addon_guard.check_call(reader,'gateway_telemetry_skill_started',self.args)

    def test_own_stats_allowed_and_foreign_stats_denied(self):
        self.assertIs(addon_guard.check_call(OWNER,'gateway_telemetry_skill_stats',{'actor_subject':OWNER.subject}),self.profile)
        with self.assertRaisesRegex(PermissionError,'foreign_telemetry'):
            addon_guard.check_call(OWNER,'gateway_telemetry_skill_stats',{'actor_subject':'yandex:9002'})

    def test_backend_write_uses_separate_operation_grant(self):
        self.assertIs(addon_guard.check_call(OWNER,'gateway_call_tool',{'tool_name':'platforma.requests.create'}),self.profile)
        self.profile['resources']=[r for r in self.profile['resources'] if r['id']!='platforma.requests.create']
        with self.assertRaises(PermissionError):addon_guard.check_call(OWNER,'gateway_call_tool',{'tool_name':'platforma.requests.create'})

    def test_colleague_with_forged_profile_write_is_still_denied(self):
        reader=GatewayActor(subject='yandex:9002',scopes=('platforma:write',))
        self.profile['subject']=reader.subject
        with self.assertRaisesRegex(PermissionError,'sandbox_participant_required'):
            addon_guard.check_call(reader,'gateway_call_tool',{'tool_name':'platforma.requests.create'})

    def test_unknown_delete_route_denied(self):
        with self.assertRaises(PermissionError):addon_guard.check_call(OWNER,'gateway_call_tool',{'tool_name':'platforma.requests.delete'})


class OwnTelemetryToolTests(unittest.TestCase):
    def setUp(self):
        class FakeMcp:
            def __init__(self):self.tools={}
            def tool(self,**kwargs):
                def register(fn):self.tools[fn.__name__]=fn;return fn
                return register
        self.mcp=FakeMcp();register_telemetry_tools(self.mcp)

    def test_delegated_stats_forces_authenticated_subject(self):
        run=Mock();run.require_scope.return_value=OWNER
        with patch('gateway_mcp.tools.telemetry.ToolRun.start',return_value=run),patch('gateway_mcp.tools.telemetry.skill_stats',return_value={'ok':True}) as stats:
            run_read(self.mcp.tools['gateway_telemetry_skill_stats'](project='platforma-training'))
        self.assertEqual(stats.call_args.kwargs['actor_subject'],OWNER.subject)

    def test_delegated_stats_cannot_select_another_actor(self):
        run=Mock();run.require_scope.return_value=OWNER
        with patch('gateway_mcp.tools.telemetry.ToolRun.start',return_value=run),patch('gateway_mcp.tools.telemetry.skill_stats') as stats:
            with self.assertRaises(PermissionError):
                run_read(self.mcp.tools['gateway_telemetry_skill_stats'](actor_subject='yandex:9002'))
        stats.assert_not_called()


if __name__=='__main__':unittest.main()
