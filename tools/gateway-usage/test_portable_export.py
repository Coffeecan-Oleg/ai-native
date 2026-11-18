import json,tempfile,unittest,plistlib
from pathlib import Path
from xml.etree import ElementTree as ET
from portable_export import normalize,run,rpc_decode,codex,send
from prepare_schedule import prepare
class Tests(unittest.TestCase):
 def test_usage(self):
  self.assertEqual(normalize({'prompt_tokens':10,'completion_tokens':2})['total_tokens'],12)
  with self.assertRaises(ValueError):normalize({'input_tokens':True,'output_tokens':2})
 def test_pending_retry_and_no_text(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);src=p/'events.jsonl'
   def event(n):return {'event_id':str(n),'usage':{'input_tokens':10,'output_tokens':2},'private_text':'NEVER_SEND'}
   src.write_text(json.dumps(event(1))+'\n');cfg={'adapter':'usage_jsonl','source_id':'device:agent','source':str(src),'state':str(p/'state.sqlite'),'project':'p','transport':{}}
   sent=[]
   def lost(x,_):sent.append(x);raise ConnectionError('lost acknowledgement')
   with self.assertRaises(ConnectionError):run(cfg,lost)
   src.write_text(src.read_text()+json.dumps(event(2))+'\n')
   run(cfg,lambda x,_:sent.append(x));self.assertEqual(sent[0],sent[1]);self.assertNotIn('NEVER_SEND',json.dumps(sent))
   self.assertEqual(run(cfg,lambda x,_:sent.append(x))['events_confirmed'],1)
   self.assertEqual(run(cfg,lambda x,_:sent.append(x))['events_confirmed'],0)
 def test_native_scheduler_files(self):
  with tempfile.TemporaryDirectory() as t:
   for name in ('macos','linux','windows'):
    p=Path(prepare(name,'/path with spaces/python','/path with spaces/export.py','/path with spaces/config.json',Path(t)/name));self.assertTrue(p.exists())
    if name=='macos':self.assertEqual(plistlib.loads(p.read_bytes())['StartInterval'],300)
    if name=='windows':ET.parse(p)
 def test_sse(self):
  self.assertEqual(rpc_decode('event: message\ndata: {"result":\ndata: {"ok":true}}\n\n','text/event-stream')['result']['ok'],True)
 def test_codex_identity_delta_and_reset(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'session';cfg={'session_id':'s','cwd':'work','project':'p','source_id':'device'}
   def write(n):
    counts={'input_tokens':n,'output_tokens':2,'total_tokens':n+2,'cached_input_tokens':0}
    p.write_text(json.dumps({'type':'session_meta','payload':{'id':'s','cwd':'work'}})+'\n'+json.dumps({'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':counts,'last_token_usage':{'input_tokens':3,'output_tokens':1,'total_tokens':4}}}})+'\n')
   write(10);batch,base=codex(p,cfg,None);self.assertEqual(json.loads(batch[0]['usage_json'])['total_tokens'],4)
   self.assertEqual(codex(p,cfg,base)[0],[])
   write(20);self.assertEqual(json.loads(codex(p,cfg,base)[0][0]['usage_json'])['total_tokens'],10)
   write(5)
   with self.assertRaises(ValueError):codex(p,cfg,base)
 def test_http_mcp_round_trip(self):
  import threading,os
  from http.server import HTTPServer,BaseHTTPRequestHandler
  from unittest.mock import patch
  calls=[]
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_POST(self):
    x=json.loads(self.rfile.read(int(self.headers['Content-Length'])));calls.append(x)
    if x['method']=='initialize':result={'protocolVersion':'2025-03-26'}
    elif x['method']=='notifications/initialized':self.send_response(202);self.end_headers();return
    else:result={'content':[{'type':'text','text':json.dumps({'ok':True,'inserted':True})}]}
    self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps({'jsonrpc':'2.0','id':x.get('id'),'result':result}).encode())
  h=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=h.serve_forever);thread.start()
  try:
   with patch.dict(os.environ,{'TEST_USAGE_TOKEN':'fixture'}):self.assertTrue(send({'event_id':'fixture'},{'kind':'mcp_http','url':'http://127.0.0.1:'+str(h.server_port),'token_env':'TEST_USAGE_TOKEN'})['ok'])
   self.assertEqual(calls[-1]['params']['name'],'gateway_usage_import')
  finally:h.shutdown();thread.join();h.server_close()
if __name__=='__main__':unittest.main()
