"""Compare synthetic saved text without permissions or external I/O."""
import hashlib,unittest
from unittest.mock import patch
from gateway_mcp.services import skill_comparison as comparison,skill_lifecycle as lifecycle
from gateway_mcp.routes.skill_lifecycle import _comparison_panel


def snapshot(revision,files=None,**metadata):
    files={'SKILL.md':'Read confirmed facts.\n',**(files or {})}
    row={'skill_id':'shared-synthetic','title':'Synthetic skill','description':'Synthetic description',
         'version':'1.0.'+str(revision),'revision':revision,'files':files,
         'manifest':{p:{'bytes':len(t.encode()),'sha256':hashlib.sha256(t.encode()).hexdigest(),'encoding':'utf-8'} for p,t in files.items()},**metadata}
    row['fingerprint']=lifecycle._fingerprint(row);return row


class SkillComparisonTests(unittest.TestCase):
    def compare(self,old,new):return comparison.compare(new,old,basis='published')

    def test_added_removed_changed_unchanged_and_metadata_are_distinct(self):
        old=snapshot(1,{'changed.md':'Old rule\n','removed.md':'Remove me\n'})
        new=snapshot(4,{'changed.md':'New rule\n','added.md':'Add me\n'},title='New name')
        r=self.compare(old,new)
        self.assertEqual(r['counts'],{'added':1,'removed':1,'changed':1,'unchanged':1})
        self.assertEqual([x['field'] for x in r['metadata']],['title','version'])
        changed=next(x for x in r['files'] if x['path']=='changed.md')
        self.assertEqual(changed['diff_status'],'complete')
        self.assertIn({'kind':'removed','text':'-Old rule'},changed['diff']);self.assertIn({'kind':'added','text':'+New rule'},changed['diff'])
        self.assertEqual(changed['before_sha256'],old['manifest']['changed.md']['sha256'])
        self.assertEqual((r['previous_revision'],r['current_revision']),(1,4));self.assertFalse(r['omitted_content'])

    def test_unchanged_text_and_metadata_does_not_fabricate_changes(self):
        r=self.compare(snapshot(1,version='1.0.0'),snapshot(2,version='1.0.0'))
        self.assertEqual(r['files'],[]);self.assertEqual(r['metadata'],[]);self.assertEqual(r['counts']['unchanged'],1)
        self.assertEqual(r['output_lines'],0)

    def test_metadata_only_edit_is_visible_without_claiming_file_changes(self):
        r=self.compare(snapshot(1),snapshot(3,description='Updated scope'))
        self.assertEqual(r['files'],[]);self.assertEqual({x['field'] for x in r['metadata']},{'description','version'})
        html=_comparison_panel(r,'shared-synthetic')
        self.assertIn('Изменения карточки',html);self.assertIn('Содержимое файлов не изменилось',html)

    def test_line_ending_or_final_newline_changes_are_explained(self):
        for text in ['Read confirmed facts.\r\n','Read confirmed facts.']:
            with self.subTest(text=repr(text)):
                r=self.compare(snapshot(1),snapshot(2,{'SKILL.md':text}))
                self.assertEqual(r['counts']['changed'],1)
                self.assertEqual(r['files'][0]['diff_status'],'line_endings_only');self.assertEqual(r['files'][0]['diff'],[])

    def test_tampered_foreign_and_future_snapshots_are_rejected(self):
        old=snapshot(1);new=snapshot(2);new['files']['SKILL.md']+='tampered'
        with self.assertRaises(ValueError):self.compare(old,new)
        for old,new in [(snapshot(1,skill_id='shared-other'),snapshot(2)),(snapshot(2),snapshot(2)),(snapshot(3),snapshot(2))]:
            with self.assertRaises(ValueError):self.compare(old,new)
        with self.assertRaises(ValueError):comparison.compare(snapshot(2),snapshot(1),basis='invented')

    def test_first_revision_has_no_invented_baseline(self):
        r=comparison.compare(snapshot(1));self.assertIsNone(r['previous_revision']);self.assertEqual(r['files'],[])
        self.assertIn('первая сохранённая редакция',_comparison_panel(r,'shared-synthetic'))

    def test_large_files_are_listed_by_hash_without_expensive_text_diff(self):
        r=self.compare(snapshot(1,{'big.txt':'a'*17000}),snapshot(2,{'big.txt':'b'*17000}))
        self.assertTrue(r['omitted_content']);self.assertEqual(r['counts']['changed'],1)
        self.assertEqual(r['files'][0]['diff_status'],'input_limit');self.assertEqual(r['output_lines'],0)
        self.assertIsNotNone(r['files'][0]['before_sha256']);self.assertIsNotNone(r['files'][0]['after_sha256'])

    def test_line_and_aggregate_input_caps_do_not_hide_file_inventory(self):
        old=snapshot(1,{'many.txt':'old\n'*201,'short.txt':'old\n'*5})
        new=snapshot(2,{'many.txt':'new\n'*201,'short.txt':'new\n'*5})
        with patch.object(comparison,'MAX_INPUT_LINES',6):r=self.compare(old,new)
        self.assertEqual(r['counts']['changed'],2);self.assertEqual(len(r['files']),2)
        self.assertTrue(r['omitted_content']);self.assertEqual(r['output_lines'],0)

    def test_output_caps_keep_all_changed_files_and_partial_notice(self):
        old=snapshot(1,{'second.md':'Old\n'});new=snapshot(2,{'SKILL.md':'New\n','second.md':'Updated\n'})
        with patch.object(comparison,'MAX_OUTPUT_LINES',1):r=self.compare(old,new)
        self.assertEqual(r['output_lines'],1);self.assertTrue(r['omitted_content']);self.assertEqual(len(r['files']),2)
        self.assertTrue(all(x['diff_status']=='output_limit' for x in r['files']))
        self.assertIn('Показаны не все строки',_comparison_panel(r,'shared-synthetic'))
        with patch.object(comparison,'MAX_OUTPUT_CHARS',1):r=self.compare(old,new)
        self.assertEqual(r['output_chars'],0);self.assertTrue(r['omitted_content'])

    def test_output_escapes_html_and_uses_exact_revision_for_before_after_files(self):
        old=snapshot(1,{'danger.md':'<script>old()</script>\n'})
        new=snapshot(8,{'danger.md':'<img src=x onerror=bad()>\n'},title='<img onerror=bad()>')
        html=_comparison_panel(self.compare(old,new),'shared-synthetic')
        self.assertNotIn('<script>old()',html);self.assertNotIn('<img',html)
        self.assertIn('&lt;script&gt;old()',html);self.assertIn('&lt;img src=x',html)
        self.assertIn('version=1&amp;path=danger.md',html);self.assertIn('version=8&amp;path=danger.md',html)
        self.assertIn('не оценка безопасности',html)

    def test_added_removed_sides_do_not_link_to_nonexistent_resource(self):
        r=self.compare(snapshot(1,{'removed.md':'Old\n'}),snapshot(2,{'added.md':'New\n'}))
        html=_comparison_panel(r,'shared-synthetic')
        self.assertIn('version=1&amp;path=removed.md',html);self.assertNotIn('version=2&amp;path=removed.md',html)
        self.assertIn('version=2&amp;path=added.md',html);self.assertNotIn('version=1&amp;path=added.md',html)

    def test_previous_saved_is_not_labelled_as_published(self):
        r=comparison.compare(snapshot(2),snapshot(1),basis='previous_saved')
        self.assertIn('могла быть черновиком',_comparison_panel(r,'shared-synthetic'))
        self.assertTrue(r['read_only']);self.assertEqual(r['basis'],'previous_saved')
