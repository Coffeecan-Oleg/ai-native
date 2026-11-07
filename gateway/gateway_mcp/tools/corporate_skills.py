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
            actor = run.require_scope()
            result = await asyncio.to_thread(operation, actor)
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
        """List the available corporate and training skills with immutable resource hashes."""
        return await perform('gateway_corporate_skill_list', {}, lambda actor: corporate_skills.list_skills(actor))

    @mcp.tool(annotations=ToolAnnotations(title='Прочитать корпоративный навык', readOnlyHint=True))
    async def gateway_corporate_skill_read(skill: str, path: str = 'SKILL.md', offset: int = 0, limit: int = 16384, check_only: bool = False) -> str:
        """Read a permitted resource, or check declared operations with check_only=true.

        The check calls no external service. A full SKILL.md read can return an
        own-actor read_receipt; pass its id as metadata_json.skill_read_id in skill
        events to link the read version. Reading does not prove execution.
        """
        args = {'skill': skill, 'path': path, 'offset': offset, 'limit': limit}
        def operation(actor):
            from gateway_mcp.services import corporate_access,skill_capabilities,skill_activity
            if type(check_only) is not bool:raise ValueError('check_only must be boolean')
            if check_only:
                if not corporate_access.enabled():raise ValueError('Capability check requires corporate entry')
                return skill_capabilities.check(actor,skill)
            result=corporate_skills.read_skill(**args,actor=actor)
            if corporate_access.enabled() and path=='SKILL.md':
                result['capabilities']=skill_capabilities.check(actor,result['skill'])
                result['read_receipt']=skill_activity.record_read(actor,result)
            return result
        return await perform('gateway_corporate_skill_read', {**args,'check_only':check_only}, operation)
