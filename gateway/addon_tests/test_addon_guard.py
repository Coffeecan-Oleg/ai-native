"""Integration boundaries for the finite training addon, using real MCP types."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gateway_mcp.services import addon_guard as guard
from gateway_mcp.services.policy import GatewayActor
from mcp.types import CallToolRequest, CallToolRequestParams, ServerResult, CallToolResult, TextContent

ROOT=Path(__file__).resolve().parents[1]
TEMPLATE=json.loads((ROOT/"addons/platforma-training/example-addon-profile.json").read_text(encoding="utf-8"))

class GuardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory())
        self.file=Path(self.tmp)/"profiles.json"
        self.profile=copy.deepcopy(TEMPLATE)
        self.profile["profiles"]["test:owner"]["expires_at"]="2099-01-01T00:00:00Z"
        self.save()
        self.enterContext(patch.dict(os.environ,{
            "GATEWAY_ADDON_PROFILES_FILE":str(self.file),
            "GATEWAY_KNOWLEDGE_VAULT_PATH":str(Path(self.tmp).resolve()/"vault"),
            "GATEWAY_TOOLS_FILE":str(ROOT/"gateway-platforma-tools.json")}))
        self.actor=GatewayActor(subject="test:owner",login="owner",scopes=("memory:read","memory:write"))
    def save(self):self.file.write_text(json.dumps(self.profile),encoding="utf-8")
    def check(self,name,args=None,actor=None):return guard.check_call(actor or self.actor,name,args or {})
    def test_allowed_context_route(self):self.assertTrue(self.check("gateway_call_tool",{"tool_name":"platforma.context.get"}))
    def test_unknown_route_denied(self):
        with self.assertRaises(PermissionError):self.check("gateway_call_tool",{"tool_name":"platforma.requests.delete"})
    def test_foreign_backend_denied(self):
        with self.assertRaises(PermissionError):self.check("gateway_call_tool",{"tool_name":"google_drive.files.get"})
    def test_unknown_actor_denied(self):
        with self.assertRaises(PermissionError):self.check("gateway_access_profile",actor=GatewayActor(subject="outsider"))
    def test_revoked_profile_old_actor_stops(self):
        self.check("gateway_access_profile")
        self.profile["profiles"]["test:owner"]["status"]="revoked";self.save()
        with self.assertRaises(PermissionError):self.check("gateway_access_profile")
    def test_expired_profile(self):
        self.profile["profiles"]["test:owner"]["expires_at"]="2000-01-01T00:00:00Z";self.save()
        with self.assertRaises(PermissionError):self.check("gateway_access_profile")
    def test_foreign_project_profile(self):
        self.profile["profiles"]["test:owner"]["project"]="APP999";self.save()
        with self.assertRaises(PermissionError):self.check("gateway_access_profile")
    def test_missing_profile_fails_closed(self):
        self.file.unlink()
        with self.assertRaises(PermissionError):self.check("gateway_access_profile")
    def test_employee_template_is_draft(self):
        p=json.loads((ROOT/"addons/platforma-training/employee-profile.draft.json").read_text())
        self.assertEqual(p["status"],"draft");self.assertIsNone(p["subject"])
    def test_foreign_space_denied(self):
        with self.assertRaises(PermissionError):self.check("gateway_knowledge_space_get",{"space":"space:foreign"})
    def test_no_global_search(self):
        with self.assertRaises(PermissionError):self.check("gateway_knowledge_search",{})
    def test_document_path_and_alias_denied(self):
        for path in ["../../private","README.md","unregistered-id","skills/other.md"]:
            with self.subTest(path=path),self.assertRaises(PermissionError):
                self.check("gateway_knowledge_document_get",{"space":"space:organizational-awareness","document":path})
    def test_registered_document_allowed(self):
        self.check("gateway_knowledge_document_get",{"space":"space:organizational-awareness","document":"awareness/start.md"})
    def test_membership_creation_denied(self):
        with self.assertRaises(PermissionError):
            self.check("gateway_knowledge_space_create",{"slug":"organizational-awareness","members_json":'[{"subject":"outsider"}]'})
    def test_only_target_container_create(self):
        self.check("gateway_knowledge_space_create",{"slug":"organizational-awareness"})
        with self.assertRaises(PermissionError):self.check("gateway_knowledge_space_create",{"slug":"foreign"})
    def test_document_id_mismatch(self):
        with self.assertRaises(PermissionError):
            self.check("gateway_knowledge_document_put",{"space":"space:organizational-awareness","path":"awareness/start.md","document_id":"overwrite-other","kind":"navigation"})
    def test_user_memory_only(self):
        for scope in ("","project","team","company"):
            with self.subTest(scope=scope),self.assertRaises(PermissionError):
                self.check("gateway_memory_write",{"scope":scope})
    def test_foreign_user_memory(self):
        with self.assertRaises(PermissionError):
            self.check("gateway_memory_search",{"scope":"user","subject":"outsider","include_sources":False,"include_yonote":False})
    def test_memory_no_implicit_sources(self):
        with self.assertRaises(PermissionError):self.check("gateway_memory_search",{"scope":"user"})
        self.check("gateway_memory_search",{"scope":"user","include_sources":False,"include_yonote":False})
    def test_memory_ttl_and_provenance(self):
        self.check("gateway_memory_write",{"scope":"user","source_type":"knowledge","ttl_days":7})
        for args in ({"ttl_days":365,"source_type":"knowledge"},{"ttl_days":7,"source_type":"manual"}):
            with self.assertRaises(PermissionError):self.check("gateway_memory_write",args)
    def test_other_usage_project_denied(self):
        with self.assertRaises(PermissionError):self.check("gateway_usage_import",{"project":"foreign"})
        self.check("gateway_usage_import",{"project":"platforma-training"})
    def test_alternative_mutating_tools_denied(self):
        for name in ("gateway_knowledge_document_delete","gateway_knowledge_member_add","gateway_knowledge_document_restore",
                     "gateway_memory_forget","gateway_admin_list_access","gateway_file_read","gateway_factory_git"):
            with self.subTest(name=name),self.assertRaises(PermissionError):self.check(name)
    def test_existing_canonical_skill_protected(self):
        from gateway_mcp.services.knowledge import create_space,put_document
        path="skills/organizational-awareness/SKILL.md"
        create_space(actor=self.actor,slug="organizational-awareness",title="Test")
        spec=self.profile["profiles"]["test:owner"]["documents"]["space:organizational-awareness"][path]
        args={"space":"space:organizational-awareness","path":path,**spec}
        self.check("gateway_knowledge_document_put",args)
        put_document(actor=self.actor,space=args["space"],title="Skill",content="# Original",path=path,**spec)
        with self.assertRaises(PermissionError):self.check("gateway_knowledge_document_put",args)
    def test_search_output_filtered_before_delivery(self):
        docs=[{"path":"awareness/start.md","space":"space:organizational-awareness"},
              {"path":"private.md","space":"space:organizational-awareness"},
              {"path":"awareness/start.md","space":"space:foreign"}]
        result=ServerResult(CallToolResult(content=[TextContent(type="text",text=json.dumps({"ok":True,"documents":docs,"count":3}))]))
        filtered=guard.filter_result(self.actor,"gateway_knowledge_search",result,self.profile["profiles"]["test:owner"])
        data=json.loads(filtered.root.content[0].text)
        self.assertEqual(data["count"],1)
    def test_handler_denies_before_effect_and_audits(self):
        effects=[]
        async def handler(request):
            effects.append("effect")
            return ServerResult(CallToolResult(content=[TextContent(type="text",text='{"ok":true}')]))
        from types import SimpleNamespace
        server=SimpleNamespace(_mcp_server=SimpleNamespace(request_handlers={CallToolRequest:handler}))
        request=CallToolRequest(method="tools/call",params=CallToolRequestParams(name="gateway_knowledge_document_delete",arguments={}))
        with patch("gateway_mcp.services.auth.current_actor",return_value=self.actor),patch("gateway_mcp.services.observability.audit_event") as audit:
            guard.register(server)
            response=asyncio.run(server._mcp_server.request_handlers[CallToolRequest](request))
        self.assertTrue(response.root.isError);self.assertEqual(effects,[])
        self.assertNotIn("content",audit.call_args.kwargs["arguments"])
    def test_readonly_knowledge_acl_still_applies(self):
        from gateway_mcp.services.knowledge import create_space,get_document
        create_space(actor=self.actor,slug="organizational-awareness",title="Test")
        with self.assertRaises(PermissionError):
            get_document(actor=GatewayActor(subject="outsider"),space="space:organizational-awareness",document="awareness/start.md")

if __name__=="__main__":unittest.main()

