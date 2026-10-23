"""Knowledge filtering must preserve the advertised MCP output contract and ACLs."""
import ast
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.utilities.func_metadata import func_metadata
from mcp.types import CallToolResult, ServerResult, TextContent

ROOT = Path(__file__).resolve().parents[1]
source = ROOT / 'gateway_mcp/services/addon_guard.py'
tree = ast.parse(source.read_text(encoding='utf-8'))
functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'filter_result', '_space'}]
namespace = {'json': json}
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)
filter_result = namespace['filter_result']
check_space = namespace['_space']
SPACE = 'space:organizational-awareness'


def string_tool() -> str:
    return ''


class KnowledgeResultContractTests(unittest.TestCase):
    def setUp(self):
        module = ModuleType('gateway_mcp.services.knowledge')
        module._personal_slug = lambda actor: 'fictional-reader'
        self.enterContext(patch.dict(sys.modules, {'gateway_mcp.services.knowledge': module}))
        self.actor = SimpleNamespace(subject='fixture:reader')
        self.profile = {'reader_only': True, 'spaces': [SPACE], 'documents': {SPACE: {'awareness/start.md': {}}}}
        self.metadata = func_metadata(string_tool)

    def result(self, payload):
        content, structured = self.metadata.convert_result(json.dumps(payload))
        return ServerResult(CallToolResult(content=content, structuredContent=structured))

    def both(self, result):
        text = json.loads(result.root.content[0].text)
        self.metadata.output_model.model_validate(result.root.structuredContent)
        structured = json.loads(result.root.structuredContent['result'])
        self.assertEqual(text, structured)
        return text

    def test_space_list_contract_and_private_space_filter(self):
        result = self.result({'ok': True, 'count': 3, 'spaces': [
            {'space': SPACE}, {'space': 'personal:fixture-owner'}, {'space': 'space:unrelated'}]})
        filtered = filter_result(self.actor, 'gateway_knowledge_space_list', result, self.profile)
        payload = self.both(filtered)
        self.assertEqual(payload['spaces'], [{'space': SPACE}])
        self.assertEqual(payload['count'], 1)
        self.assertNotIn('fixture-owner', json.dumps(filtered.model_dump()))

    def test_search_contract_filters_paths_and_foreign_spaces(self):
        result = self.result({'ok': True, 'count': 3, 'documents': [
            {'space': SPACE, 'path': 'awareness/start.md'},
            {'space': SPACE, 'path': 'private/unapproved.md'},
            {'space': 'personal:fixture-owner', 'path': 'awareness/start.md'}]})
        payload = self.both(filter_result(self.actor, 'gateway_knowledge_search', result, self.profile))
        self.assertEqual(payload['documents'], [{'space': SPACE, 'path': 'awareness/start.md'}])
        self.assertEqual(payload['count'], 1)
        self.assertNotIn('fixture-owner', json.dumps(result.model_dump()))
        self.assertNotIn('unapproved', json.dumps(result.model_dump()))

    def test_owner_keeps_own_space_but_cannot_see_foreign_spaces(self):
        profile = {**self.profile, 'reader_only': False}
        result = self.result({'ok': True, 'count': 3, 'spaces': [
            {'space': SPACE}, {'space': 'personal:fictional-reader'}, {'space': 'personal:fixture-owner'}]})
        payload = self.both(filter_result(self.actor, 'gateway_knowledge_space_list', result, profile))
        self.assertEqual([s['space'] for s in payload['spaces']], [SPACE, 'personal:fictional-reader'])

    def test_empty_or_foreign_space_still_denied(self):
        for space in ['', 'organizational-awareness', 'personal:fixture-owner', 'space:unrelated']:
            with self.subTest(space=space), self.assertRaisesRegex(PermissionError, '^addon_space_denied$'):
                check_space(self.profile, self.actor, space)
        self.assertEqual(check_space(self.profile, self.actor, SPACE), SPACE)

    def test_unrestricted_result_is_unchanged(self):
        result = self.result({'ok': True, 'spaces': [{'space': SPACE}]})
        before = result.model_dump()
        self.assertIs(filter_result(self.actor, 'gateway_knowledge_space_list', result, None), result)
        self.assertEqual(result.model_dump(), before)

    def test_unrelated_tool_is_unchanged(self):
        result = self.result({'ok': True, 'value': 'fixture'})
        before = result.model_dump()
        self.assertIs(filter_result(self.actor, 'gateway_access_profile', result, self.profile), result)
        self.assertEqual(result.model_dump(), before)

    def test_error_does_not_retain_old_structured_payload(self):
        result = ServerResult(CallToolResult(content=[TextContent(type='text', text='{"ok": false, "error": "fixture"}')],
                                            structuredContent={'result': 'unfiltered-fixture'}, isError=True))
        filtered = filter_result(self.actor, 'gateway_knowledge_space_list', result, self.profile)
        self.assertTrue(filtered.root.isError)
        self.assertIsNone(filtered.root.structuredContent)
        self.assertNotIn('unfiltered-fixture', json.dumps(filtered.model_dump()))


if __name__ == '__main__':
    unittest.main()
