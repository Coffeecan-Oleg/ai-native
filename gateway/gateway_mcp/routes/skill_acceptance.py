"""Escaped read-only presentation of acceptance examples after authorization."""
from html import escape
from gateway_mcp.services import skill_acceptance as service

def panel(report,*,skill_id,version,published):
    e=lambda x:escape(str(x),quote=True)
    body='<section class="panel skill-acceptance"><h2>Контрольные примеры навыка</h2>'
    messages={
        'missing':'Файл test-cases.json не приложен. Качество ответа по примерам здесь не проверено. Автор может добавить 3–5 вымышленных задач в следующую редакцию.',
        'invalid':'Не удалось разобрать test-cases.json. Проверьте схему 1, от 3 до 5 примеров, уникальные ID, ожидаемые факты и запрещённые действия. Лимит файла — 16 КиБ.',
        'stale':'Примеры относятся к другому SHA256 SKILL.md. Автору нужно пересмотреть задачи и указать контрольную сумму этой инструкции. Готовые запросы не показаны.',
        'ready':'Формат примеров корректен и привязан к этой инструкции. Качество ответа ещё не оценено: сравните ответ ассистента с критериями вручную.'}
    body+='<p>'+messages[report['state']]+'</p>'
    if report['state']!='ready':return body+'</section>'
    body+='<p class="skill-code">Версия: '+e(version)+'. SHA256 SKILL.md: <code>'+e(report['skill_sha256'])+'</code>.</p>'
    body+='<p>Не используйте реальные адреса, пароли или сведения клиентов. Платформа не запускает модель и не считает чтение файла успешным прохождением проверки.</p>'
    if not published:
        body+='<p>Это закрытая сохранённая редакция. MCP выдаёт только опубликованный материал, поэтому запросы для неё не формируются. Автор и владелец могут прочитать инструкцию и критерии здесь; не публикуйте черновик только ради проверки.</p>'
    for case in report['cases']:
        body+='<details><summary>'+e(case['id'])+' — '+e(case['title'])+'</summary>'
        body+='<h3>Вымышленные исходные данные</h3><pre class="skill-material">'+e(case['input'])+'</pre>'
        for label,key in [('В ответе должны сохраниться','expected_facts'),('Недопустимые действия','forbidden_actions')]:
            body+='<h3>'+label+'</h3><ul>'+''.join('<li>'+e(x)+'</li>' for x in case[key])+'</ul>'
        if published:
            body+='<label>Готовый запрос для ассистента<textarea class="skill-call" readonly>'+e(service.prompt(skill_id,version,report,case))+'</textarea></label>'
        body+='<p>После ответа отметьте вручную: каждый факт сохранён; запрещённых действий нет; неизвестные сведения не придуманы. При несоответствии верните навык автору на доработку.</p></details>'
    return body+'</section>'
