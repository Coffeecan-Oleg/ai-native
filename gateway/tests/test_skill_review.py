"""Advisory snapshot checks. Synthetic text; no external I/O or permissions."""
import hashlib,json,unittest
from unittest.mock import patch

from gateway_mcp.services import skill_review as review,skill_lifecycle as lifecycle
from gateway_mcp.routes.skill_lifecycle import _review_panel

HEADER='---\nname: sample\ndescription: Safe sample\nversion: 1.0.0\n---\n'


def snapshot(files=None,version='1.0.0'):
    files={'SKILL.md':HEADER+'Read the guide.\n',**(files or {})}
    row={'title':'Synthetic skill','description':'Safe sample','version':version,'revision':3,'files':files,
         'manifest':{p:{'bytes':len(t.encode()),'sha256':hashlib.sha256(t.encode()).hexdigest(),'encoding':'utf-8'} for p,t in files.items()}}
    row['fingerprint']=lifecycle._fingerprint(row)
    return row


def codes(result):return [i['code'] for i in result['issues']]


class SkillReviewTests(unittest.TestCase):
    def test_relative_links_images_encoded_names_and_reference_definitions(self):
        data=snapshot({'SKILL.md':HEADER+'[guide](docs/guide.md "Read") ![image](pic.svg)\n[late][label] [label][] [label]\n[label]: <docs/guide.md>\n',
            'docs/guide.md':'[root](../SKILL.md) [space](<a%20b.md>) [anchor](#section)\n',
            'docs/a b.md':'Content.\n','pic.svg':'<svg/>\n'})
        r=review.inspect_snapshot(data)
        self.assertEqual(r['issues'],[]);self.assertEqual(r['local_file_links_checked'],7)
        self.assertEqual(r['anchors_unchecked'],1);self.assertEqual(r['snapshot_revision'],3)
        self.assertEqual(r['fingerprint'],data['fingerprint']);self.assertTrue(r['scan_complete'])

    def test_missing_files_and_reference_locations_are_useful_without_raw_targets(self):
        r=review.inspect_snapshot(snapshot({'SKILL.md':HEADER+'[missing](private-token-do-not-echo.md)\n[other][missing-label]\n[ordinary prose]\n'}))
        self.assertEqual(codes(r),['file_missing','reference_missing'])
        self.assertEqual([i['line'] for i in r['issues']],[6,7])
        self.assertNotIn('private-token-do-not-echo',json.dumps(r));self.assertEqual(r['attention_count'],2)

    def test_code_blocks_inline_code_indentation_and_escaped_brackets_are_ignored(self):
        body='''`[code](missing.md)` ``[code](missing.md)``
~~~md
[code](missing.md)
~~~
````md
```
[code](missing.md)
````
    [code](missing.md)
\t[code](missing.md)
\\[escaped](missing.md)
\\![escaped](missing.md)
[^footnote] [^note][missing]
'''
        r=review.inspect_snapshot(snapshot({'SKILL.md':HEADER+body}))
        self.assertEqual(r['issues'],[]);self.assertEqual(r['links_seen'],0)

    def test_escape_backslashes_and_inline_code_with_different_tick_lengths(self):
        body='``outer `[hidden](missing.md)` outer`` [real](SKILL.md)\n\\\\[real](SKILL.md)\n` unclosed [real](SKILL.md)\n'
        r=review.inspect_snapshot(snapshot({'SKILL.md':HEADER+body}))
        self.assertEqual(r['issues'],[]);self.assertEqual(r['local_file_links_checked'],3)

    def test_paths_cannot_escape_package_and_server_paths_are_not_disk_reads(self):
        r=review.inspect_snapshot(snapshot({'SKILL.md':HEADER+'[x](../secrets) [x](%2e%2e/secrets) [x](..%5csecrets) [x](/admin)\n'}))
        self.assertEqual(codes(r),['link_outside_package']*3+['server_path_unchecked'])
        self.assertEqual(r['local_file_links_checked'],0)

    def test_external_links_and_anchors_never_fetch_or_claim_service_health(self):
        text='[site](https://example.invalid/?token=do-not-echo) [mail](mailto:user@example.invalid) [relative](//example.invalid/x) [script](javascript:bad) [anchor](SKILL.md#part) [invalid](https://[bad)\n<a href="unknown">HTML</a>\n'
        with patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')) as network:
            r=review.inspect_snapshot(snapshot({'SKILL.md':HEADER+text}))
        network.assert_not_called();self.assertFalse(r['network_calls']);self.assertFalse(r['permissions_checked'])
        self.assertEqual(r['external_links_unchecked'],4);self.assertEqual(r['anchors_unchecked'],1)
        self.assertCountEqual(codes(r),['link_scheme_unchecked','link_invalid','html_links_unchecked'])
        self.assertNotIn('do-not-echo',json.dumps(r));self.assertEqual(r['publication_decision'],'owner_review')

    def test_explicit_version_mismatch_and_optional_or_bad_headers(self):
        self.assertIn('version_mismatch',codes(review.inspect_snapshot(snapshot(version='1.0.1'))))
        for content in ['No header.\n','---\nname: sample\ndescription: Safe\n---\nBody.\n']:
            self.assertNotIn('version_mismatch',codes(review.inspect_snapshot(snapshot({'SKILL.md':content},version='2.0'))))
        for content in ['---\nname: [\n---\nBody.\n','---\nname: &anchor sample\ndescription: Safe\n---\nBody.\n']:
            self.assertIn('header_invalid',codes(review.inspect_snapshot(snapshot({'SKILL.md':content}))))

    def test_optional_requirements_use_existing_strict_schema_without_grants(self):
        ops=[{'id':'read','title':'Read lists','kind':'route','route':'unisender.getLists'}]
        r=review.inspect_snapshot(snapshot({'requirements.json':json.dumps({'schema':1,'operations':ops})}))
        self.assertEqual(r['requirements'],'valid');self.assertEqual(r['operations'],ops)
        self.assertFalse(r['permissions_checked']);self.assertEqual(r['issues'],[])
        self.assertEqual(review.inspect_snapshot(snapshot())['requirements'],'missing')

    def test_invalid_large_and_deep_requirements_are_advisory_not_server_errors(self):
        samples=['{secret-invalid','x'*8193,'['*1500+']'*1500,json.dumps({'schema':True,'operations':[]}),
                 json.dumps({'schema':1,'operations':[{'id':'x','title':'Test','kind':[]}]}),
                 json.dumps({'schema':1,'operations':[{'id':'x','title':'Test','kind':'assistant','route':'bad'}]})]
        for content in samples:
            with self.subTest(size=len(content)):
                r=review.inspect_snapshot(snapshot({'requirements.json':content}))
                self.assertEqual(r['requirements'],'invalid');self.assertEqual(r['operations'],[])
                self.assertIn('requirements_invalid',codes(r));self.assertNotIn('secret-invalid',json.dumps(r))

    def test_legacy_address_is_found_in_code_and_non_markdown_as_information(self):
        r=review.inspect_snapshot(snapshot({'SKILL.md':HEADER+'`https://KROMIN.AXION-TEST.AKMETRON.RU/mcp`\n',
            'settings.json':'{"url":"https://kromin.axion-test.akmetron.ru/mcp"}\n'}))
        self.assertEqual(codes(r),['legacy_address','legacy_address']);self.assertEqual(r['attention_count'],0)

    def test_tampered_material_is_rejected_before_analysis(self):
        row=snapshot();row['files']['SKILL.md']+='changed'
        with self.assertRaises(ValueError):review.inspect_snapshot(row)

    def test_report_and_link_work_are_bounded_and_partial_status_is_explicit(self):
        r=review.inspect_snapshot(snapshot({'SKILL.md':HEADER+'[missing](absent.md)\n'*600}))
        self.assertEqual(len(r['issues']),review.MAX_ISSUES);self.assertTrue(r['issues_truncated'])
        self.assertFalse(r['scan_complete']);self.assertEqual(r['links_seen'],review.MAX_LINKS)
        self.assertEqual(r['issue_count'],501)
        self.assertIn('Проверка выполнена частично',_review_panel(r,'shared-synthetic'))

    def test_line_cap_across_files_and_long_lines_do_not_claim_complete_scan(self):
        with patch.object(review,'MAX_LINES',7):
            r=review.inspect_snapshot(snapshot({'SKILL.md':HEADER+'Two\nlines\n','next.md':'[bad](missing.md)\n'}))
        self.assertFalse(r['scan_complete']);self.assertIn('scan_limit',codes(r));self.assertEqual(r['lines_checked'],7)
        r=review.inspect_snapshot(snapshot({'SKILL.md':HEADER+'x'*8193+'[bad](missing.md)\n'}))
        self.assertFalse(r['scan_complete']);self.assertEqual(r['links_seen'],0)

    def test_panel_escapes_untrusted_names_and_declaration_and_binds_exact_revision(self):
        files={'requirements.json':json.dumps({'schema':1,'operations':[{'id':'text','title':'<img src=x onerror=bad()>','kind':'assistant'}]}),
            '<img onerror=bad()>.md':'[missing](no-file.md)\n'}
        r=review.inspect_snapshot(snapshot(files));html=_review_panel(r,'shared-synthetic')
        self.assertNotIn('<img src=x',html);self.assertNotIn('<img onerror',html)
        self.assertIn('&lt;img src=x onerror=bad()&gt;',html)
        self.assertIn('version=3',html);self.assertIn('path=%3Cimg',html)
        self.assertIn('не подтверждает подключение сервиса',html)
