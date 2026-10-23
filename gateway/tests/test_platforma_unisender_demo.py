import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from gateway_mcp.backends import call_backend
from gateway_mcp.backends.common import BackendRouteError
from gateway_mcp.backends.platforma_demo_store import DATA_FILE
from gateway_mcp.backends.unisender_demo import CAMPAIGN_ID, ROUTE_NAME

ROOT=Path(__file__).resolve().parents[1]
ROUTE=next(r for r in json.loads((ROOT/'gateway-platforma-tools.json').read_text(encoding='utf-8'))['tools'] if r['name']==ROUTE_NAME)


def call(arguments,route=None):
    coroutine=call_backend(route or ROUTE,arguments)
    try:
        coroutine.send(None)
    except StopIteration as done:return done.value
    finally:coroutine.close()
    raise AssertionError('Fictional campaign attempted asynchronous I/O')


class UniSenderDemoTests(unittest.TestCase):
    def test_exact_fixture_is_labelled_and_exposes_only_aggregate_counters(self):
        data=call({'campaign_id':CAMPAIGN_ID})['data']
        self.assertTrue(data['synthetic']);self.assertTrue(data['read_only'])
        self.assertFalse(data['real_unisender_connected']);self.assertFalse(data['upstream_contacted'])
        self.assertEqual(data['statistics'],{'total':100,'sent':100,'delivered':98,'read_unique':25,'read_all':30,
            'clicked_unique':7,'clicked_all':9,'unsubscribed':1,'spam':0})
        self.assertEqual(data['source'],'platforma-training:unisender-demo#'+CAMPAIGN_ID)
        for value in data['statistics'].values():self.assertIs(type(value),int)

    def test_numeric_foreign_path_null_and_malformed_ids_are_denied(self):
        for identifier in ('42','LAB-UNISENDER-002','../../.env','*','',None,42,True,['42']):
            with self.subTest(id=identifier),self.assertRaises(BackendRouteError):call({'campaign_id':identifier})

    def test_only_campaign_id_argument_is_accepted(self):
        for key in ('api_key','endpoint','url','recipients','send','method','subject','body'):
            with self.subTest(key=key),self.assertRaises(BackendRouteError):
                call({'campaign_id':CAMPAIGN_ID,key:'fictional-input'})
        with self.assertRaises(BackendRouteError):call({})

    def test_forged_write_route_or_real_service_cannot_use_fixture_handler(self):
        for change in ({'http_method':'POST'},{'scope':'platforma:write'},{'name':'unisender.campaign.stats'},
                       {'name':'platforma.unisender-demo.send'},{'name':'platforma.unisender-demo.campaigns.list'}):
            with self.subTest(change=change),self.assertRaises(BackendRouteError):call({'campaign_id':CAMPAIGN_ID},ROUTE|change)

    def test_test_credentials_do_not_enable_live_network_or_appear_in_output(self):
        with patch.dict(os.environ,{'UNISENDER_API_KEY':'fictional-never-use','GATEWAY_UNISENDER_API_KEY':'fictional-never-use'}),\
             patch('httpx.AsyncClient',side_effect=AssertionError('Network forbidden')):
            result=call({'campaign_id':CAMPAIGN_ID})
        self.assertNotIn('fictional-never-use',json.dumps(result))

    def test_caller_mutation_does_not_change_fixture_or_original_platforma_data(self):
        before=hashlib.sha256(DATA_FILE.read_bytes()).hexdigest()
        first=call({'campaign_id':CAMPAIGN_ID})['data'];first['statistics']['sent']=9000
        self.assertEqual(call({'campaign_id':CAMPAIGN_ID})['data']['statistics']['sent'],100)
        self.assertEqual(hashlib.sha256(DATA_FILE.read_bytes()).hexdigest(),before)

    def test_delivery_gap_is_not_misrepresented_as_an_error_counter(self):
        data=call({'campaign_id':CAMPAIGN_ID})['data']
        self.assertNotIn('errors',data['statistics'])
        self.assertEqual(data['unsupported_metrics'],['delivery_errors'])
        self.assertIn('нельзя',data['metric_note'])


if __name__=='__main__':unittest.main()
