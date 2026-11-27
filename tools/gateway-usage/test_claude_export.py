import json,tempfile,unittest
from pathlib import Path
from portable_export import run,anthropic_usage,claude
class ClaudeTests(unittest.TestCase):
 def test_cache_accounting(self):
  u=anthropic_usage({'input_tokens':10,'output_tokens':5,'cache_read_input_tokens':100,'cache_creation_input_tokens':20})
  self.assertEqual((u['input_tokens'],u['total_tokens']),(130,135))
  self.assertEqual(u['input_tokens_details']['cached_tokens'],100)
 def test_invalid(self):
  for u in ({'input_tokens':True,'output_tokens':0},{'input_tokens':-1,'output_tokens':0},{'input_tokens':1}):
   with self.assertRaises(ValueError):anthropic_usage(u)
 def test_stream_dedup_scope_privacy(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);src=p/'s.jsonl';cfg={'adapter':'claude_code_jsonl','source_id':'device','session_id':'s','cwd':'work','project':'p','source':str(src),'state':str(p/'state'),'transport':{}}
   def row(stop,session='s',cwd='work'):
    return {'type':'assistant','sessionId':session,'cwd':cwd,'message':{'id':'msg1','model':'claude','stop_reason':stop,'usage':{'input_tokens':10,'output_tokens':5},'content':'PRIVATE'}}
   src.write_text(json.dumps(row(None))+'\n');sent=[]
   self.assertEqual(run(cfg,lambda e,_:sent.append(e))['events_confirmed'],0)
   src.write_text(json.dumps(row('end_turn'))+'\n'+json.dumps(row('end_turn'))+'\n'+json.dumps(row('end_turn','other'))+'\n')
   self.assertEqual(run(cfg,lambda e,_:sent.append(e))['events_confirmed'],1)
   self.assertEqual(run(cfg,lambda e,_:sent.append(e))['events_confirmed'],0)
   self.assertNotIn('PRIVATE',json.dumps(sent))
   src.write_text(json.dumps(row('end_turn',cwd='other'))+'\n')
   with self.assertRaises(ValueError):claude(src,cfg)
 def test_api_final_and_retry(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);src=p/'events';src.write_text(json.dumps({'event_id':'request1','final':True,'usage':{'input_tokens':3,'output_tokens':2}})+'\n')
   cfg={'adapter':'anthropic_jsonl','source_id':'sdk','source':str(src),'project':'p','state':str(p/'state'),'transport':{}}
   sent=[]
   def fail(e,_):sent.append(e);raise ConnectionError()
   with self.assertRaises(ConnectionError):run(cfg,fail)
   run(cfg,lambda e,_:sent.append(e));self.assertEqual(sent[0],sent[1])
   self.assertEqual(run(cfg,lambda e,_:None)['events_confirmed'],0)
if __name__=='__main__':unittest.main()
