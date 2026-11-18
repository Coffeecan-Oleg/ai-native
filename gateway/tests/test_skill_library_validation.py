import io
import stat
import unittest
import zipfile
from gateway_mcp.services import skill_library as service

SKILL = b'---\nname: demo\ndescription: Training material\nversion: "1.0.0"\n---\n\nOnly read fictional data.\n'


def archive(entries):
    result=io.BytesIO()
    with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in entries:
            z.writestr(name,data)
    return result.getvalue()


class SkillValidationTests(unittest.TestCase):
    def test_markdown_and_plain_markdown_preserve_content_hashes(self):
        for blob in [SKILL,'# Простая инструкция\nТолько учебные данные.'.encode()]:
            parsed=service.parse_upload('инструкция.md',blob)
            self.assertEqual(parsed['files']['SKILL.md'],blob)
            self.assertEqual(parsed['manifest']['SKILL.md']['bytes'],len(blob))

    def test_single_folder_zip_keeps_support_files_as_inert_text(self):
        source=b'raise RuntimeError("must never execute")'
        parsed=service.parse_upload('demo.zip',archive([('demo/SKILL.md',SKILL),('demo/scripts/example.py',source)]))
        self.assertEqual(set(parsed['files']),{'SKILL.md','scripts/example.py'})
        self.assertEqual(parsed['files']['scripts/example.py'],source)

    def test_archive_paths_and_special_files_are_rejected(self):
        for bad in ['../evil.md','/evil.md','C:/evil.md','sub\\evil.md','.env','sub/../evil.md','sub//evil.md']:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                blob=archive([('SKILL.md',SKILL),(bad,b'x')])
                # ZipInfo normalizes Windows separators when creating fixtures.
                if '\\' in bad:
                    blob=blob.replace(bad.replace('\\','/').encode(),bad.encode())
                service.parse_upload('bad.zip',blob)
        info=zipfile.ZipInfo('link.md');info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
        with self.assertRaises(ValueError):
            service.parse_upload('bad.zip',archive([('SKILL.md',SKILL),(info,b'/etc/passwd')]))

    def test_duplicate_case_multi_skill_and_siblings_rejected(self):
        for entries in [[('SKILL.md',SKILL),('skill.md',b'x')], [('one/SKILL.md',SKILL),('two/SKILL.md',SKILL)],
                        [('one/SKILL.md',SKILL),('outside.md',b'x')]]:
            with self.subTest(entries=entries),self.assertRaises(ValueError):
                service.parse_upload('bad.zip',archive(entries))

    def test_bounded_expansion_and_file_count(self):
        for blob in [archive([('SKILL.md',b'x'*(service.MAX_FILE+1))]),
                     archive([('SKILL.md',SKILL)]+[(f'{n}.txt',b'x') for n in range(service.MAX_FILES)])]:
            with self.assertRaises(ValueError):service.parse_upload('bad.zip',blob)
        with self.assertRaises(ValueError):service.parse_upload('demo.md',b'x'*(service.MAX_PACKAGE+1))

    def test_binary_unsupported_and_bad_yaml_are_rejected(self):
        for filename,blob in [('bad.exe',SKILL),('bad.md',b'\xff\x00'),('bad.md',b''),
            ('bad.md',b'---\nname: [bad]\ndescription: test\n---\nbody'),
            ('bad.md',b'---\nname: a\ndescription: !!python/object/apply:os.system [echo forbidden]\n---\nbody'),
            ('bad.md',b'---\nname: &a test\ndescription: *a\n---\nbody')]:
            with self.subTest(filename=filename,blob=blob),self.assertRaises(ValueError):service.parse_upload(filename,blob)

    def test_title_unicode_and_control_validation(self):
        self.assertEqual(service.clean_title('  Разбор   заявок  '),'Разбор заявок')
        self.assertEqual(service.title_key('ＡＢ'),'ab')
        for text in ['x',' ', 'тест\nимя', 'тест\u200b', 'x'*121]:
            with self.subTest(text=text),self.assertRaises(ValueError):service.clean_title(text)


if __name__=='__main__':unittest.main()
