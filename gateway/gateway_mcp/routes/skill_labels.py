"""Names from the viewer's visible catalogue; historical IDs remain usable."""
from html import escape
from urllib.parse import urlencode

from gateway_mcp.services import corporate_skills


def visible_titles(actor):
    try:
        return {row['name']: row['title'] for row in corporate_skills.list_skills(actor)['skills']}
    except Exception:
        # A disabled/unavailable catalogue must not hide the audit record, or
        # reveal private drafts by consulting a less restricted source.
        return {}


def skill_label(skill_id, titles):
    skill_id = str(skill_id or '—')
    title = titles.get(skill_id)
    if not title:
        return '<code>' + escape(skill_id) + '</code>'
    url = '/platforma/skills/view?' + urlencode({'id': skill_id})
    return ('<a href="' + escape(url, quote=True) + '"><strong>' + escape(title)
            + '</strong></a><br><small class="muted">' + escape(skill_id) + '</small>')
