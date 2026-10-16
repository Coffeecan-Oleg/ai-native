"""Real HTTP MCP protocol against isolated fixtures, never the live Gateway."""
import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from starlette.testclient import TestClient
from gateway_mcp.services import auth,policy
from gateway_mcp.services.knowledge import put_document

ROOT=Path(__file__).resolve().parents[1]

class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory())
        self.temp=Path(self.tmp).resolve()
        self.file=self.temp/"profiles.json"
        self.profiles=json.loads((ROOT/"addons/platforma-training/example-addon-profile.json").read_text(encoding="utf-8"))
        self.profiles["profiles"]["test:owner"]["expires_at"]="2099-01-01T00:00:00Z"
        self.save()
        scopes=("skills:read","access:request","tools:read","tools:call","platforma:read",
                "telemetry:write","telemetry:read","memory:read","memory:write")
        profile=self.profiles["profiles"]["test:owner"]
        tools=tuple(r["id"][4:] for r in profile["resources"] if r["kind"]=="tool")
        routes=tuple(r["id"] for r in profile["resources"] if r["kind"]=="backend_route")
        self.enterContext(patch.dict(os.environ,{
            "GATEWAY_AUTH_ENABLED":"true","GATEWAY_JWT_SECRET":"isolated-only-signing-key-"*4,
            "GATEWAY_ISSUER_URL":"https://gateway.example.invalid",
            "GATEWAY_RESOURCE_URL":"https://gateway.example.invalid/mcp",
            "GATEWAY_DATABASE_URL":"","GATEWAY_AUDIT_LOG":str(self.temp/"audit.jsonl"),
            "GATEWAY_RESOURCE_POLICY_MODE":"strict",
            "GATEWAY_KNOWLEDGE_VAULT_PATH":str(self.temp/"vault"),
            "GATEWAY_ADDON_PROFILES_FILE":str(self.file),
            "GATEWAY_TOOLS_FILE":str(ROOT/"gateway-platforma-tools.json")}))
        self.enterContext(patch.object(policy,"list_active_scope_grants",return_value=[]))
        self.enterContext(patch.object(auth,"gateway_oauth_access_token_revoked",return_value=False))
        self.actor=policy.GatewayActor(subject="test:owner",login="owner",scopes=scopes,scope_ceiling=scopes,tool_allowlist=tools+routes)
        self.token=auth.mint_gateway_token(self.actor)
        from gateway_mcp.server import create_mcp
        server=create_mcp()
        self.http=self.enterContext(TestClient(server.streamable_http_app()))
        self.headers={"Authorization":"Bearer "+self.token,"Accept":"application/json, text/event-stream"}
        result=self.rpc({"id":1,"method":"initialize","params":{
            "protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"isolated-addon-test","version":"1"}}})
        self.headers["MCP-Protocol-Version"]=result["result"]["protocolVersion"]
        self.rpc({"method":"notifications/initialized"})
        self.seq=10
    def save(self):self.file.write_text(json.dumps(self.profiles),encoding="utf-8")
    def rpc(self,body):
        response=self.http.post("/mcp",json={"jsonrpc":"2.0",**body},headers=self.headers)
        self.assertIn(response.status_code,(200,202),response.text[:300])
        if response.headers.get("mcp-session-id"):self.headers["Mcp-Session-Id"]=response.headers["mcp-session-id"]
        if not response.content:return {}
        if "text/event-stream" in response.headers.get("content-type",""):
            payloads=[json.loads(s[5:].strip()) for s in response.text.splitlines() if s.startswith("data:")]
            return next(s for s in payloads if "result" in s or "error" in s)
        return response.json()
    def tool(self,name,args=None):
        self.seq+=1
        envelope=self.rpc({"id":self.seq,"method":"tools/call","params":{"name":name,"arguments":args or {}}})
        self.assertNotIn("error",envelope,envelope)
        result=envelope["result"]
        text=next(x["text"] for x in result["content"] if x["type"]=="text")
        try:value=json.loads(text)
        except ValueError:value={"error_text":text}
        return result,value
    def create(self):
        r,d=self.tool("gateway_knowledge_space_create",{"slug":"organizational-awareness","title":"Test","members_json":"[]"})
        self.assertFalse(r.get("isError"));return d["space"]["space"]
    def test_knowledge_read_write_search_and_usage(self):
        space=self.create()
        bundle=json.loads((ROOT/"addons/platforma-training/awareness-bundle.json").read_text(encoding="utf-8"))
        doc=bundle["documents"][0]
        r,d=self.tool("gateway_knowledge_document_put",{**doc,"space":space})
        self.assertFalse(r.get("isError"),d)
        r,d=self.tool("gateway_knowledge_document_get",{"space":space,"document":doc["path"]})
        self.assertIn("Организационная осведомлённость",d["document"]["content"])
        # A document outside the allowed catalogue must be filtered on the server.
        put_document(actor=self.actor,space=space,title="Private sentinel",content="# PRIVATE_NOT_ALLOWED",
                     path="unregistered.md",document_id="fixture-other")
        r,d=self.tool("gateway_knowledge_search",{"space":space,"query":"","limit":20})
        self.assertTrue(d["documents"])
        self.assertNotIn("PRIVATE_NOT_ALLOWED",json.dumps(d))
        self.assertTrue(all(x["path"] in self.profiles["profiles"]["test:owner"]["documents"][space] for x in d["documents"]))
        r,d=self.tool("gateway_usage_dashboard",{"project":"platforma-training"})
        self.assertTrue(any(x["metric"]=="context_volume" for x in d["rows"]),d)
        self.assertTrue(all(x["total_tokens"] is None for x in d["rows"]))
    def test_exactly_once_usage_and_no_foreign_actors(self):
        args={"event_id":"isolated-fixture-event","project":"platforma-training","model":"fixture",
              "usage_json":json.dumps({"input_tokens":10,"output_tokens":2})}
        self.assertTrue(self.tool("gateway_usage_import",args)[1]["inserted"])
        self.assertFalse(self.tool("gateway_usage_import",args)[1]["inserted"])
        d=self.tool("gateway_usage_dashboard",{})[1]
        self.assertEqual(sum(x["total_tokens"] or 0 for x in d["rows"]),12)
        from gateway_mcp.services.usage_meter import dashboard
        self.assertEqual(dashboard(policy.GatewayActor(subject="outsider"))["rows"],[])
    def test_denial_alternative_paths_and_revocation(self):
        for name,args in [("gateway_knowledge_space_get",{"space":"space:foreign"}),
                          ("gateway_memory_search",{"scope":"project","subject":"foreign","include_sources":False,"include_yonote":False}),
                          ("gateway_call_tool",{"tool_name":"platforma.requests.delete"}),
                          ("gateway_knowledge_member_add",{"space":"space:organizational-awareness","subject":"outsider","role":"writer"}),
                          ("gateway_admin_list_access",{})]:
            with self.subTest(name=name):
                result,data=self.tool(name,args);self.assertTrue(result.get("isError"),data)
        self.profiles["profiles"]["test:owner"]["status"]="revoked";self.save()
        r,d=self.tool("gateway_usage_dashboard",{})
        self.assertTrue(r.get("isError"));self.assertEqual(d["error"],"addon_inactive_profile")
    def test_existing_skill_and_wrong_personal_path_denied(self):
        space=self.create()
        bundle=json.loads((ROOT/"addons/platforma-training/awareness-bundle.json").read_text(encoding="utf-8"))
        doc=next(d for d in bundle["documents"] if d["path"]=="skills/organizational-awareness/SKILL.md")
        self.assertFalse(self.tool("gateway_knowledge_document_put",{**doc,"space":space})[0].get("isError"))
        r,d=self.tool("gateway_knowledge_document_put",{**doc,"space":space,"content":"# overwrite"})
        self.assertTrue(r.get("isError"));self.assertEqual(d["error"],"addon_existing_skill_protected")
        r,d=self.tool("gateway_knowledge_document_get",{"space":"personal:outsider","document":"usage/client-setup.md"})
        self.assertTrue(r.get("isError"))
    def test_platforma_reads_preserve_resource_policy(self):
        from gateway_mcp.services import access_policy
        grant={"id":1,"actions":["read"],"effect":"allow","resource_pattern":"*"}
        arguments={"tool_name":"platforma.requests.list","arguments_json":json.dumps({"period":"2026-10","overdue_only":True,"limit":100})}
        with patch.object(access_policy,"list_active_resource_grants",return_value=[grant]):
            r,d=self.tool("gateway_call_tool",arguments)
        self.assertFalse(r.get("isError"),d);self.assertEqual(d["data"]["total"],39)
        self.assertTrue(all(x["source"].startswith("platforma-training:") for x in d["data"]["records"]))
        with patch.object(access_policy,"list_active_resource_grants",return_value=[]):
            r,d=self.tool("gateway_call_tool",arguments)
        self.assertTrue(r.get("isError"),d)
if __name__=="__main__":unittest.main()

