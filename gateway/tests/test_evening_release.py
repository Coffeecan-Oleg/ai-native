import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gateway_mcp import config
from gateway_mcp.services import corporate_access as access
from gateway_mcp.services.policy import GatewayActor


class ImageRevisionTests(unittest.TestCase):
    def test_immutable_image_revision_overrides_legacy_compose_label(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'revision';p.write_text('a'*40+'\n')
            with patch.object(config,'IMAGE_REVISION_FILE',p),patch.dict(os.environ,{'GATEWAY_RELEASE_SHA':'old-runtime'}):
                self.assertEqual(config.release_sha(),'a'*40)

    def test_invalid_file_is_redacted_instead_of_claiming_old_release(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'revision';p.write_text('PRIVATE INVALID VALUE')
            with patch.object(config,'IMAGE_REVISION_FILE',p),patch.dict(os.environ,{'GATEWAY_RELEASE_SHA':'old-runtime'}):
                self.assertEqual(config.release_sha(),'unknown')

    def test_legacy_images_keep_bounded_safe_label(self):
        with patch.object(config,'IMAGE_REVISION_FILE',Path('/nonexistent-gateway-revision-12345')),patch.dict(os.environ,{'GATEWAY_RELEASE_SHA':'old+overlay-1'}):
            self.assertEqual(config.release_sha(),'old+overlay-1')
        with patch.object(config,'IMAGE_REVISION_FILE',Path('/nonexistent-gateway-revision-12345')),patch.dict(os.environ,{'GATEWAY_RELEASE_SHA':'secret value / token'}):
            self.assertEqual(config.release_sha(),'dev')


class LifecyclePathBoundaryTests(unittest.TestCase):
    def test_employee_has_only_exact_new_browser_paths(self):
        actor=GatewayActor(subject='synthetic-employee',scopes=('skills:read',))
        with patch.object(access,'fresh_actor',return_value=actor),patch.object(access,'is_owner',return_value=False):
            for path in ['/platforma/skills/manage','/platforma/skills/manage/action','/platforma/skills/manage/edit']:
                self.assertTrue(access.web_path_allowed(actor,path))
            for path in ['/admin/users','/platforma/skills/manage/other','/credentials']:
                self.assertFalse(access.web_path_allowed(actor,path))

    def test_mcp_token_cannot_become_management_browser_session(self):
        actor=GatewayActor(subject='synthetic-employee',tool_allowlist=('gateway_access_profile',))
        with patch.object(access,'fresh_actor',return_value=actor),patch.object(access,'is_owner',return_value=False):
            self.assertFalse(access.web_path_allowed(actor,'/platforma/skills/manage'))
