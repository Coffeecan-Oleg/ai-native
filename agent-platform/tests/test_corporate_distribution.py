import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform.plugins.builder import build_plugins, build_claude, build_cursor_rules, build_portable
from agent_platform.plugins.gateway import write_gateway_plugin
from agent_platform.cursor_distribution import build_cursor_distribution

ROOT = Path(__file__).resolve().parents[2]
URL = "https://platforma.akmetron.ru/mcp"


class CorporateDistributionTests(unittest.TestCase):
    def test_all_connection_formats_use_oauth_and_one_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/"gateway-mcp"
            write_gateway_plugin(root, "test", corporate={"profile":"corporate","mcpUrl":URL})
            for name in ("mcp.json", "codex.mcp.json", "cursor.mcp.json", ".mcp.json"):
                data = json.loads((root/name).read_text(encoding="utf-8"))
                server = data["mcpServers"]["gateway"]
                self.assertEqual(server["url"], URL, name)
                self.assertNotIn("headers", server)
                self.assertNotIn("scope", server)
                if name == "mcp.json":
                    self.assertEqual(server["extensions"]["com.openai"]["auth"],
                                     {"type": "oauth", "client": {"mode": "dcr"}})
            portable = json.loads((root/"plugin.json").read_text(encoding="utf-8"))
            overlay = json.loads((root/".codex-plugin/plugin.json").read_text(encoding="utf-8"))
            self.assertEqual(portable["name"], "gateway-mcp")
            self.assertEqual(portable["version"], overlay["version"])
            self.assertEqual(portable["extensions"]["com.openai"]["interface"], overlay["interface"])
            self.assertLessEqual(len(overlay["interface"]["shortDescription"]), 30)
            for file in root.rglob("*.json"):
                text = file.read_text(encoding="utf-8")
                self.assertNotIn("GATEWAY_MCP_TOKEN", text)
                self.assertNotIn("Bearer", text)
                self.assertNotIn("memory:write", text)
                self.assertNotIn("configuration", json.loads(text))

    def test_unsafe_or_incomplete_corporate_config_fails_before_write(self):
        for config in ({}, {"profile":"typo","mcpUrl":URL},
            *({"profile":"corporate","mcpUrl":url} for url in
              ("http://example.invalid/mcp", "https://user:secret@example.invalid/mcp",
               "https://example.invalid/mcp?token=secret", "https://example.invalid/mcp#x", "https://example.invalid/", None))):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/"gateway-mcp"
                with self.assertRaises(ValueError):
                    write_gateway_plugin(root, corporate=config)
                self.assertFalse(root.exists())

    def _fixture(self, base):
        source=base/"skills"
        for name in ("current", "old", "reference"):
            folder=source/name;folder.mkdir(parents=True)
            (folder/"SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n", encoding="utf-8")
        packs=[{"name":name,"displayName":name,"description":name,"category":"AI","skills":[name],**extra}
            for name,extra in (("current",{}),("old",{"distribution":"historical"}),("reference",{"distribution":"reference"}))]
        return source, {"packs":packs,"gateway":{"profile":"corporate","mcpUrl":URL}}

    def test_historical_packs_stay_readable_but_are_not_recommended(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);source,registry=self._fixture(base);out=base/"out"
            build_plugins(source,registry,out,clean=False)
            for name in (".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json", ".cursor-plugin/marketplace.json", ".zcode-plugin/marketplace.json"):
                entries=json.loads((out/name).read_text(encoding="utf-8"))["plugins"]
                self.assertNotIn("old",[p["name"] for p in entries])
                self.assertNotIn("reference",[p["name"] for p in entries])
            self.assertTrue((out/"plugins/old/skills/old/SKILL.md").exists())
            self.assertIn("Исторический",(out/"plugins/old/README.md").read_text(encoding="utf-8"))
            root_text=(out/"__init__.py").read_text(encoding="utf-8")
            self.assertNotIn("'old':",root_text)
            installers=base/"installers";installers.mkdir()
            for name in ("install.sh","install.ps1"):(installers/name).write_text("test",encoding="utf-8")
            build_cursor_distribution(out/"plugins",base/"cursor",installers,"test")
            names=[p["name"] for p in json.loads((base/"cursor/latest.json").read_text())["plugins"]]
            self.assertEqual(names,["current","gateway-mcp"])

    def test_portable_and_claude_exports_follow_recommended_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);source,registry=self._fixture(base);out=base/"out"
            build_claude(source,registry,out,False)
            self.assertEqual([p.name for p in (out/"claude/.claude/skills").iterdir()],["current"])
            build_cursor_rules(source,out,False,registry=registry)
            self.assertEqual([p.name for p in (out/"cursor/.cursor/rules").iterdir()],["skill-current.mdc"])
            for target in ("opencode","openclaw","hermes","zcode"):
                build_portable(target,source,out,False,registry=registry)
                self.assertEqual([p.name for p in (out/target/"skills").iterdir()],["current"])

    def test_rebuild_is_deterministic_and_overwrites_stale_oauth_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);source,registry=self._fixture(base);first=base/"first";second=base/"second"
            build_plugins(source,registry,first,False)
            stale=first/"plugins/gateway-mcp/codex.mcp.json"
            stale.write_text('{"old":"token"}')
            build_plugins(source,registry,first,False)
            build_plugins(source,registry,second,False)
            a={p.relative_to(first).as_posix():p.read_bytes() for p in first.rglob("*") if p.is_file()}
            b={p.relative_to(second).as_posix():p.read_bytes() for p in second.rglob("*") if p.is_file()}
            self.assertEqual(a,b)

    def test_repository_selects_corporate_endpoint_and_historical_training(self):
        registry=json.loads((ROOT/"agent-platform/skill-packs.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["gateway"],{"profile":"corporate","mcpUrl":URL})
        old=next(p for p in registry["packs"] if p["name"]=="platforma-training")
        self.assertEqual(old["distribution"],"historical")
        self.assertEqual(old["mcpUrl"],URL)


if __name__ == "__main__": unittest.main()
