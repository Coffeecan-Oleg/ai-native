"""Check declarations against current routing/auth. No API calls or secret reads."""
import hashlib,json,re
from pathlib import Path
from gateway_mcp.config import read_json,tools_file
from gateway_mcp.services import corporate_skills,corporate_access,skill_library
from gateway_mcp.services.policy import has_scope

REGISTRY=Path(__file__).resolve().parents[1]/'help_assets/skill-operations.json'
REQUIREMENTS='requirements.json'
IDENTIFIER=re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
ROUTE=re.compile(r'[A-Za-z0-9_.-]{1,120}\Z')


def manifest_hash(row):
    values={p:s['sha256'] for p,s in row['files'].items()}
    return hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def validate_operations(data):
    if not isinstance(data,dict) or set(data)!={'schema','operations'} or type(data['schema']) is not int or data['schema']!=1:
        raise ValueError('Неверная схема requirements.json.')
    rows=data['operations']
    if not isinstance(rows,list) or not 1<=len(rows)<=10:raise ValueError('Нужно от 1 до 10 операций.')
    seen=set()
    for item in rows:
        if not isinstance(item,dict) or set(item)-{'id','title','kind','route'}:raise ValueError('Неверные поля операции.')
        if not isinstance(item.get('id'),str) or not IDENTIFIER.fullmatch(item['id']) or item['id'] in seen:raise ValueError('Неверный ID операции.')
        seen.add(item['id'])
        title=item.get('title')
        if not isinstance(title,str) or not 2<=len(title)<=160 or any(ord(c)<32 for c in title):raise ValueError('Неверное название операции.')
        if item.get('kind') not in {'assistant','route','unavailable'}:raise ValueError('Неверный вид операции.')
        if item['kind']=='route' and (not isinstance(item.get('route'),str) or not ROUTE.fullmatch(item['route'])):raise ValueError('Неверный маршрут.')
        if item['kind']!='route' and 'route' in item:raise ValueError('Маршрут допустим только для route.')
    return rows


def _declaration(actor,row):
    registry=json.loads(REGISTRY.read_text(encoding='utf-8'))
    entry=registry.get('skills',{}).get(row['name']) if registry.get('schema')==1 else None
    if entry and entry.get('manifest_sha256')==manifest_hash(row):
        return validate_operations({'schema':1,'operations':entry['operations']}),'reviewed_registry'
    if REQUIREMENTS in row['files']:
        if row['files'][REQUIREMENTS]['bytes']>8192:raise ValueError('requirements.json больше 8 КиБ.')
        result=corporate_skills.read_skill(row['name'],REQUIREMENTS,actor=actor)
        if result['sha256']!=row['files'][REQUIREMENTS]['sha256']:raise ValueError('Версия требований изменилась.')
        return validate_operations(json.loads(result['content'])),'author_declaration'
    return [],'registry_stale' if entry else 'requirements_missing'


def check(actor,identifier):
    if not has_scope(actor,'skills:read'):raise PermissionError('skills:read required')
    rows=corporate_skills.list_skills(actor)['skills']
    matches=[r for r in rows if r['name']==identifier]
    if not matches and isinstance(identifier,str):matches=[r for r in rows if skill_library.title_key(r['title'])==skill_library.title_key(identifier)]
    if len(matches)!=1:raise PermissionError('Unknown corporate skill')
    row=matches[0]
    result={'ok':True,'skill':row['name'],'skill_version':row['version'],'manifest_sha256':manifest_hash(row),
        'read_only':True,'external_service_checked':False,'scope':'declared_operations_only',
        'notice':'Проверены заявленные операции, конфигурация и права. Ключи и внешние API не запрашивались. Полный смысл инструкции автоматически не проверяется.'}
    try:operations,source=_declaration(actor,row)
    except (ValueError,KeyError,TypeError,OSError):
        operations=[];source='requirements_invalid'
    result.update(requirements_source=source,operations=[])
    routes=read_json(tools_file(),{'tools':[]}).get('tools',[])
    for op in operations:
        status='not_connected';reason='Серверная операция не подключена.'
        if op['kind']=='assistant':status='assistant_task';reason='Текстовая работа в вашем ИИ-приложении; сервер её не выполняет.'
        elif op['kind']=='route':
            name=op['route'];route=next((r for r in routes if r.get('name')==name),None)
            if route:
                if not has_scope(actor,'tools:call') or not has_scope(actor,route.get('scope','')):
                    status='missing_scope';reason='Текущему подключению не хватает права на маршрут.'
                elif actor.tool_allowlist is not None and not {'gateway_call_tool',name}<=set(actor.tool_allowlist):
                    status='token_ceiling';reason='Маршрут не включён в разрешения этого MCP-подключения.'
                else:
                    from gateway_mcp.services import addon_guard
                    try:
                        addon_guard.check_call(actor,'gateway_call_tool',{'tool_name':name})
                        status='route_allowed';reason='Маршрут разрешён. Доступ к конкретному объекту и ответ внешнего сервиса проверяются при вызове.'
                    except PermissionError:status='access_denied';reason='Маршрут сейчас не разрешён серверными правилами или выключен.'
                    except Exception:status='unavailable';reason='Не удалось проверить серверные правила.'
        result['operations'].append({**op,'status':status,'reason':reason})
    result['summary']='checked' if operations else source
    return result
