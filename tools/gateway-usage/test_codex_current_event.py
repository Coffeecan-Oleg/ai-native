import json,tempfile,unittest
from pathlib import Path
from portable_export import codex

class CodexCurrentEventTests(unittest.TestCase):
    def test_legacy_invalid_counters_are_not_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/"selected.jsonl"
            cfg={"session_id":"s","cwd":"work","project":"p","source_id":"device"}
            def event(i,o,total):
                u={"input_tokens":i,"output_tokens":o,"total_tokens":total,"cached_input_tokens":0}
                return {"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":u,"last_token_usage":u}}}
            rows=[{"type":"session_meta","payload":{"id":"s","cwd":"work"}},event(1,2,999),event(10,2,12)]
            p.write_text("\n".join(map(json.dumps,rows))+"\n",encoding="utf-8")
            batch,base=codex(p,cfg,None)
            self.assertEqual(len(batch),1)
            self.assertEqual(json.loads(batch[0]["usage_json"])["total_tokens"],12)
    def test_invalid_latest_event_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/"selected.jsonl";cfg={"session_id":"s","cwd":"work","project":"p","source_id":"device"}
            u={"input_tokens":10,"output_tokens":2,"total_tokens":99}
            p.write_text(json.dumps({"type":"session_meta","payload":{"id":"s","cwd":"work"}})+"\n"+
                         json.dumps({"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":u,"last_token_usage":u}}})+"\n",encoding="utf-8")
            with self.assertRaises(ValueError):codex(p,cfg,None)
    def test_mixed_models_are_not_assigned_to_latest_model(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/"selected.jsonl";cfg={"session_id":"s","cwd":"work","project":"p","source_id":"device"}
            u={"input_tokens":10,"output_tokens":2,"total_tokens":12}
            rows=[{"type":"session_meta","payload":{"id":"s","cwd":"work"}},
                  {"type":"turn_context","payload":{"model":"model-a"}},
                  {"type":"turn_context","payload":{"model":"model-b"}},
                  {"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":u,"last_token_usage":u}}}]
            p.write_text("\n".join(map(json.dumps,rows))+"\n",encoding="utf-8")
            self.assertEqual(codex(p,cfg,None)[0][0]["model"],"mixed-or-unknown")
if __name__=="__main__":unittest.main()

