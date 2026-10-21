"""Read-only distribution; no server-side subprocess or document rendering."""
import asyncio
import json
from mcp.types import ToolAnnotations
from gateway_mcp.services import corporate_skills
from gateway_mcp.tools.runtime import ToolRun


def register_corporate_skill_tools(mcp):
    async def perform(name, arguments, operation):
        run = ToolRun.start(tool=name, system='corporate-skills', scope='skills:read', arguments=arguments)
        try:
            run.require_scope()
            result = await asyncio.to_thread(operation)
            run.finish()
            return json.dumps(result, ensure_ascii=False)
        except PermissionError as exc:
            run.denied(exc)
            raise
        except Exception as exc:
            run.error(exc)
            raise

    @mcp.tool(annotations=ToolAnnotations(title='Корпоративные навыки Акметрон', readOnlyHint=True))
    async def gateway_corporate_skill_list() -> str:
        """List five corporate formatting skills and their immutable resource hashes."""
        return await perform('gateway_corporate_skill_list', {}, corporate_skills.list_skills)

    @mcp.tool(annotations=ToolAnnotations(title='Прочитать корпоративный навык', readOnlyHint=True))
    async def gateway_corporate_skill_read(skill: str, path: str = 'SKILL.md', offset: int = 0, limit: int = 16384) -> str:
        """Read one whitelisted skill resource: text or bounded binary base64 chunks. Never execute it."""
        args = {'skill': skill, 'path': path, 'offset': offset, 'limit': limit}
        return await perform('gateway_corporate_skill_read', args, lambda: corporate_skills.read_skill(**args))
