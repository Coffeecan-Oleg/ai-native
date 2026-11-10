import unittest
from unittest.mock import patch
from support import install_dependency_stubs

install_dependency_stubs()
from gateway_mcp.routes import skill_labels
from gateway_mcp.routes.admin_telemetry import _skill_rows
from gateway_mcp.services.skill_library import _publication_metadata


class SkillPresentationTests(unittest.TestCase):
    def test_only_visible_catalogue_is_used_and_failure_keeps_history(self):
        actor = object()
        with patch.object(skill_labels.corporate_skills, 'list_skills', return_value={
            'skills': [{'name': 'shared-visible', 'title': 'Сводка инцидента'}]
        }) as listing:
            titles = skill_labels.visible_titles(actor)
            listing.assert_called_once_with(actor)
        self.assertEqual(titles, {'shared-visible': 'Сводка инцидента'})
        self.assertNotIn('<a', skill_labels.skill_label('shared-private', titles))
        for error in (PermissionError(), RuntimeError('database unavailable')):
            with patch.object(skill_labels.corporate_skills, 'list_skills', side_effect=error):
                self.assertEqual(skill_labels.visible_titles(actor), {})

    def test_names_are_escaped_ids_retained_and_time_is_moscow(self):
        html = _skill_rows([{'skill_id': 'shared-a', 'last_seen_at': '2026-11-08T09:07:00Z'}],
                           {'shared-a': '<script>bad</script>'})
        self.assertIn('08.11.2026 12:07 МСК', html)
        self.assertIn('/platforma/skills/view?id=shared-a', html)
        self.assertIn('shared-a', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertIn('—', _skill_rows([{'skill_id': 'old', 'last_seen_at': 'bad'}]))

    def test_review_is_explicit_and_never_grants_execution(self):
        for row in ({}, {'reviewed': False}, {'reviewed': 'true'}):
            data = _publication_metadata(row)
            self.assertFalse(data['reviewed'])
            self.assertEqual(data['trust'], 'user_uploaded_unreviewed')
        data = _publication_metadata({'reviewed': True, 'publication_state': 'published'})
        self.assertTrue(data['reviewed'])
        self.assertEqual(data['trust'], 'user_uploaded_owner_reviewed')
        self.assertIn('not a security guarantee', data['review_notice'])
        self.assertIn('grants no permissions', data['review_notice'])
