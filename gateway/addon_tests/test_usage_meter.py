import unittest,tempfile,os,json
from types import SimpleNamespace
from unittest.mock import patch
from gateway_mcp.services import usage_meter as u
class Tests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'GATEWAY_KNOWLEDGE_VAULT_PATH':self.tmp.name});self.env.start();self.actor=SimpleNamespace(subject='owner',login='owner')
 def tearDown(self):self.env.stop();self.tmp.cleanup()
 def test_counts_dont_double_add_details(self):
  a=u.normalize_usage({'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,'prompt_tokens_details':{'cached_tokens':50},'completion_tokens_details':{'reasoning_tokens':5}})
  self.assertEqual(a['total_count'],120);self.assertEqual(a['cached'],50)
 def test_invalid_counts(self):
  for body in [{'input_tokens':-1,'output_tokens':2},{'input_tokens':True,'output_tokens':2},{'input_tokens':1,'output_tokens':2,'total_tokens':9}]:
   with self.assertRaises(ValueError):u.normalize_usage(body)
 def test_dedup_and_actor_isolation(self):
  args=dict(actor=self.actor,event_id='r1',project='p',metric='model_usage',quality='provider_reported',source='llm_proxy',in_count=10,out_count=2,total_count=12)
  self.assertTrue(u.insert(**args));self.assertFalse(u.insert(**args))
  self.assertEqual(u.dashboard(self.actor)['rows'][0]['total_tokens'],12)
  self.assertEqual(u.dashboard(SimpleNamespace(subject='outsider'))['rows'],[])
 def test_context_is_separate_and_no_content_stored(self):
  result=SimpleNamespace(root=SimpleNamespace(isError=False,content=[SimpleNamespace(type='text',text='{"ok":true,"document":{"content":"PRIVATE_SENTINEL"}}')]))
  u.context_observation(self.actor,'gateway_knowledge_document_get',{'space':'space:p'},result,'r')
  r=u.dashboard(self.actor)['rows'][0];self.assertEqual(r['metric'],'context_volume');self.assertIsNone(r['total_tokens']);self.assertGreater(r['returned_bytes'],0)
  self.assertNotIn(b'PRIVATE_SENTINEL',u.db_path().read_bytes())
 def test_credentials_and_errors_not_measured(self):
  result=SimpleNamespace(root=SimpleNamespace(isError=False,content=[SimpleNamespace(type='text',text='secret')]))
  self.assertFalse(u.context_observation(self.actor,'gateway_credentials_get',{},result,'r'))
  result.root.isError=True;self.assertFalse(u.context_observation(self.actor,'gateway_knowledge_document_get',{},result,'r'))
 def test_test_events_excluded(self):
  u.provider_usage(self.actor,'fixture',{'usage':{'input_tokens':3,'output_tokens':1},'model':'fixture'},is_test=True)
  self.assertEqual(u.dashboard(self.actor)['rows'],[])
 def test_missing_usage_is_unknown(self):
  u.provider_usage(self.actor,'missing',{})
  r=u.dashboard(self.actor)['rows'][0];self.assertEqual(r['quality'],'unavailable');self.assertIsNone(r['total_tokens'])
if __name__=='__main__':unittest.main()
