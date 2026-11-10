import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import development
from release_control.core import Refused,encoded,sha
from release_control.linux import validate_development_scope

class DevelopmentTests(unittest.TestCase):
    def test_scope_is_one_separate_database_and_project(self):
        cfg=development.configuration(Path('/safe/development'))
        validate_development_scope(cfg)
        self.assertEqual(cfg['database'],'gateway_mcp_dev')
        self.assertNotEqual(cfg['project'],'platforma')
    def test_production_database_or_project_cannot_be_targeted(self):
        cfg=development.configuration(Path('/safe/development'))
        for key,value in [('project','platforma'),('database','gateway_mcp'),('db_user','postgres'),('gateway_service','postgres'),('worker_service','platforma-release-worker.service')]:
            bad=copy.deepcopy(cfg);bad[key]=value
            with self.subTest(key=key),self.assertRaises(Refused):validate_development_scope(bad)
    def test_development_cannot_modify_host_caddy(self):
        cfg=development.configuration(Path('/safe/development'));cfg['caddy']['container']=None
        with self.assertRaisesRegex(Refused,'edge_scope'):validate_development_scope(cfg)
    def test_generated_env_does_not_inherit_corporate_secrets(self):
        with patch.dict(os.environ,{'UNISENDER_API_KEY':'never-copy-this-marker','GATEWAY_JWT_SECRET':'production-marker'}):
            text=development.make_env('new-private-db-key','new-private-jwt-key')
        self.assertNotIn('never-copy-this-marker',text);self.assertNotIn('production-marker',text)
        self.assertNotIn('UNISENDER_API_KEY',text);self.assertNotIn('YANDEX',text)
        self.assertNotIn('GATEWAY_BITRIX_CLIENT_SECRET',text)
        self.assertIn('GATEWAY_AUTH_ENABLED=true',text)
    def test_changed_bootstrap_plan_is_rejected_before_docker_or_writes(self):
        p={'base':'/not-created','expires':99999999999};entry={'payload':p,'sha256':sha(encoded(p))}
        with patch.object(development,'require_host'),patch.object(development,'plan') as plan:
            with self.assertRaisesRegex(Refused,'hash_mismatch'):development._apply(Path('/source'),entry,'0'*64)
            plan.assert_not_called()
    def test_expired_bootstrap_is_rejected(self):
        p={'base':'/not-created','expires':0};entry={'payload':p,'sha256':sha(encoded(p))}
        with patch.object(development,'require_host'),patch.object(development,'plan') as plan:
            with self.assertRaisesRegex(Refused,'expired'):development._apply(Path('/source'),entry,entry['sha256'])
            plan.assert_not_called()
    def test_fresh_bootstrap_only_publishes_loopback_edge(self):
        images={r:{'id':'sha256:'+'a'*64} for r in [development.GATEWAY,'postgres:16-alpine',development.CADDY]}
        text=development.compose(images)
        self.assertIn('127.0.0.1:18081:8080',text)
        self.assertNotIn('0.0.0.0:',text)
        self.assertNotIn('docker.sock',text)
        self.assertNotIn('privileged:',text)
    def test_unconfigured_auth_is_reported_honestly_in_configuration(self):
        text=development.make_env('new-db','new-jwt')
        self.assertIn('GATEWAY_BITRIX_EMBED_ENABLED=false',text)
        self.assertIn('GATEWAY_LOGIN_PROVIDER=bitrix24',text)
        self.assertNotIn('local.synthetic',text)

if __name__=='__main__':unittest.main()
