import json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import oauth_bridge as bridge

class OAuthBridgeTests(unittest.TestCase):
    def setUp(self):
        self.cfg={"gateway":"https://gateway.example.invalid","source_id":"device:session","project":"platforma-training"}
        self.reply={"scope":" ".join(sorted(bridge.SCOPES)),"token_type":"Bearer","access_token":"fixture-access",
                    "refresh_token":"fixture-refresh","expires_in":3600}
    def test_scopes_are_numeric_client_only(self):
        self.assertEqual(bridge.SCOPES,{"skills:read","access:request","telemetry:write"})
        p=bridge.token_payload(self.cfg,self.reply)
        self.assertGreater(p["expires_at"],time.time())
    def test_expanded_or_missing_scope_rejected(self):
        for scope in ("*","telemetry:write",self.reply["scope"]+" memory:write"):
            with self.subTest(scope=scope),self.assertRaises(PermissionError):
                bridge.token_payload(self.cfg,{**self.reply,"scope":scope})
    def test_bad_expiry_rejected(self):
        for expiry in (True,-1,0,"3600",float("inf")):
            with self.subTest(expiry=expiry),self.assertRaises(ValueError):
                bridge.token_payload(self.cfg,{**self.reply,"expires_in":expiry})
    def test_cross_origin_or_plain_http_rejected(self):
        for url in ("http://gateway.example.invalid/oauth/token","https://foreign.example.invalid/oauth/token",
                    "https://owner:secret@gateway.example.invalid/oauth/token"):
            with self.subTest(url=url),self.assertRaises(ValueError):
                bridge.same_origin(self.cfg,url)
    def test_wrong_tool_and_project_rejected_before_store(self):
        for command in ({"name":"gateway_knowledge_document_put","arguments":{}},
                        {"name":"gateway_usage_import","arguments":{"event_id":"id","project":"foreign","model":"m","usage_json":"{}"}}):
            with self.assertRaises(ValueError):bridge.call(self.cfg,command)
    def test_refresh_rotation_preserves_binding(self):
        previous={**bridge.token_payload(self.cfg,self.reply),"client_id":"fixture-client","token_endpoint":self.cfg["gateway"]+"/oauth/token"}
        with patch.object(bridge,"json_request",return_value={**self.reply,"refresh_token":"fixture-rotated"}),patch.object(bridge,"save") as save:
            result=bridge.refresh(self.cfg,previous)
        self.assertEqual(result["refresh_token"],"fixture-rotated")
        self.assertEqual(result["source_id"],"device:session");save.assert_called_once()
    def test_numeric_rebuild_drops_content_and_preserves_id(self):
        args={"event_id":"stable-event","project":"platforma-training","model":"actual-model",
              "usage_json":json.dumps({"input_tokens":5,"output_tokens":2,"private_text":"DO_NOT_SEND"})}
        token={"access_token":"fixture","expires_at":time.time()+1000}
        from contextlib import nullcontext
        with patch.object(bridge,"lock",return_value=nullcontext()),patch.object(bridge,"load",return_value=token),patch.object(bridge,"mcp",return_value={"ok":True}) as call:
            bridge.call(self.cfg,{"name":"gateway_usage_import","arguments":args})
        sent=call.call_args.args[-1]
        self.assertEqual(sent["event_id"],"stable-event")
        self.assertNotIn("DO_NOT_SEND",json.dumps(sent))
    @unittest.skipUnless(os.name=="nt","Windows DPAPI")
    def test_dpapi_roundtrip_is_not_plaintext(self):
        plain=b"synthetic-token-for-local-test"
        encrypted=bridge.protect(plain)
        self.assertNotIn(plain,encrypted)
        self.assertEqual(bridge.protect(encrypted,decrypt=True),plain)
    @unittest.skipUnless(os.name=="nt","Windows DPAPI")
    def test_store_binding_and_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg={**self.cfg,"oauth_store":str(Path(directory)/"private.bin")}
            data=bridge.token_payload(cfg,self.reply);bridge.save(cfg,data)
            self.assertEqual(bridge.load(cfg)["access_token"],"fixture-access")
            with self.assertRaises(PermissionError):bridge.load({**cfg,"source_id":"different"})
if __name__=="__main__":unittest.main()

