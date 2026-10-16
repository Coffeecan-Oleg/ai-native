import json
from pathlib import Path
import tempfile
import unittest

from agent_platform.plugins.builder import write_pack_mcp
from agent_platform.plugins.manifests import pack_plugin_manifest


class OwnPackTests(unittest.TestCase):
    def setUp(self):
        self.pack = {
            "name": "platforma-training", "version": "0.1.0",
            "description": "Read fictional service requests.",
            "shortDescription": "Сервисы и заявки: чтение",
            "displayName": "Платформа — учебный помощник", "category": "Productivity",
            "author": {"name": "Coffeecan-Oleg", "url": "https://github.com/Coffeecan-Oleg"},
            "mcpUrl": "https://195-133-14-140.sslip.io/mcp",
        }

    def test_own_author_and_oauth_connection_are_packaged_without_credentials(self):
        manifest = pack_plugin_manifest(self.pack)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_pack_mcp(root, self.pack, manifest)
            portable = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
            mcp = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(portable["author"]["name"], "Coffeecan-Oleg")
            self.assertEqual(portable["extensions"]["com.openai"]["interface"]["developerName"], "Coffeecan-Oleg")
            self.assertLessEqual(len(portable["extensions"]["com.openai"]["interface"]["shortDescription"]), 30)
            self.assertNotIn("mcpServers", portable)
            self.assertEqual(mcp["mcpServers"]["gateway"], {"type": "streamable-http", "url": self.pack["mcpUrl"]})
            self.assertEqual(manifest["mcpServers"], "./codex.mcp.json")
            self.assertNotIn("configuration", manifest)

    def test_packs_without_connection_keep_the_default_author_and_no_new_files(self):
        pack = {key: value for key, value in self.pack.items() if key not in {"mcpUrl", "author"}}
        manifest = pack_plugin_manifest(pack)
        self.assertEqual(manifest["author"]["name"], "coMind")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_pack_mcp(root, pack, manifest)
            self.assertEqual(list(root.iterdir()), [])
            self.assertNotIn("mcpServers", manifest)

    def test_credentials_and_non_https_endpoints_are_rejected_before_writing(self):
        for url in ("http://example.invalid/mcp", "https://user:password@example.invalid/mcp", "https://example.invalid/mcp?token=private"):
            pack = {**self.pack, "mcpUrl": url}
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with self.assertRaises(ValueError):
                    write_pack_mcp(root, pack, pack_plugin_manifest(pack))
                self.assertEqual(list(root.iterdir()), [])
