import copy
import unittest

from gateway_mcp.services.platforma_clients import (
    client_identifiers, extend_owner_telemetry_labels, telemetry_agents, telemetry_clients,
)


class ClientRegistryTests(unittest.TestCase):
    def test_owner_update_is_additive_and_does_not_change_data_permissions(self):
        before={'agents':['codex','custom-existing'],'clients':['codex-desktop'],
                'project':'platforma-training','scopes':['telemetry:write'],
                'skills':{'example':{'pack':'bounded','version':'1'}}}
        saved=copy.deepcopy(before)
        after=extend_owner_telemetry_labels(before)
        self.assertEqual(before,saved)
        self.assertEqual(after['scopes'],before['scopes'])
        self.assertEqual(after['skills'],before['skills'])
        self.assertEqual(after['project'],before['project'])
        self.assertIn('custom-existing',after['agents'])
        self.assertTrue({'opencode','openclaw'} <= set(after['agents']) & set(after['clients']))
        self.assertNotIn('*',after['agents']+after['clients'])

    def test_registry_results_cannot_mutate_global_labels(self):
        pairs=client_identifiers();pairs[0]['agent']='unknown'
        self.assertNotIn('unknown',telemetry_agents())
        self.assertEqual(set(telemetry_clients()),{v['client'] for v in client_identifiers()})
