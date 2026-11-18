"""Synthetic manual examples; no model, execution, network or database."""
import copy,hashlib,json,unittest
from gateway_mcp.services import skill_acceptance as service
from gateway_mcp.routes.skill_acceptance import panel

ROOT_SHA=hashlib.sha256(b'synthetic skill').hexdigest()
def examples():
    return {'schema':1,'skill_sha256':ROOT_SHA,'cases':[{'id':'case-'+str(i),'title':'Вымышленная задача '+str(i),
        'input':'Компания ПримерЛаб. Дата 15 декабря, ссылка неизвестна.',
        'expected_facts':['Название сохранено','Отсутствие ссылки отмечено'],
        'forbidden_actions':['Отправка писем','Выдуманная ссылка']} for i in range(3)]}
def inspect(data):return service.inspect(json.dumps(data,ensure_ascii=False),ROOT_SHA)

class AcceptanceExamplesTests(unittest.TestCase):
    def test_optional_examples_never_claim_execution_or_quality(self):
        missing=service.inspect(None,ROOT_SHA);ready=inspect(examples())
        self.assertEqual(missing['state'],'missing');self.assertEqual(ready['state'],'ready')
        self.assertEqual(len(ready['cases']),3);self.assertEqual(ready['skill_sha256'],ROOT_SHA)
        self.assertFalse(ready['executed']);self.assertFalse(ready['quality_evaluated']);self.assertTrue(ready['read_only'])
        self.assertEqual(ready['file_sha256'],hashlib.sha256(json.dumps(examples(),ensure_ascii=False).encode()).hexdigest())

    def test_changed_instruction_suppresses_stale_prompts_and_case_content(self):
        r=service.inspect(json.dumps(examples()),'f'*64)
        self.assertEqual(r['state'],'stale');self.assertEqual(r['cases'],[])
        text=panel(r,skill_id='shared-demo',version='2',published=True)
        self.assertNotIn('textarea',text);self.assertIn('другому SHA256',text)

    def test_schema_must_be_exact_integer_and_expected_fields(self):
        for value in [None,[],True,{'schema':True,**{k:v for k,v in examples().items() if k!='schema'}},examples()|{'schema':2},examples()|{'extra':True}]:
            with self.subTest(value=type(value)):self.assertEqual(inspect(value)['state'],'invalid')

    def test_case_count_bounds(self):
        for count in [0,2,3,5,6]:
            data=examples();data['cases']=[data['cases'][0]|{'id':'case-'+str(i)} for i in range(count)]
            self.assertEqual(inspect(data)['state'],'ready' if 3<=count<=5 else 'invalid')

    def test_case_fields_must_be_exact(self):
        for bad in [None,[],examples()['cases'][0]|{'run':'script.py'},{k:v for k,v in examples()['cases'][0].items() if k!='input'}]:
            data=examples();data['cases'][0]=bad;self.assertEqual(inspect(data)['state'],'invalid')

    def test_unique_bounded_ascii_case_ids(self):
        for key in ['case-1','../escape','A','x'*65,True,'']:
            data=examples();data['cases'][0]['id']=key;self.assertEqual(inspect(data)['state'],'invalid')

    def test_titles_and_inputs_are_bounded_and_controls_rejected(self):
        for field,value in [('title','x'*121),('title','two\nlines'),('input','x'*3001),('input',''),('input','a\x00b'),('input',4)]:
            data=examples();data['cases'][0][field]=value;self.assertEqual(inspect(data)['state'],'invalid')

    def test_criteria_are_nonempty_bounded_lists(self):
        for field in ['expected_facts','forbidden_actions']:
            for value in [[],['x']*9,'text',[None],['x'*301]]:
                data=examples();data['cases'][0][field]=value;self.assertEqual(inspect(data)['state'],'invalid')

    def test_raw_utf8_size_limit_and_surrogates(self):
        self.assertEqual(service.inspect(' '*service.MAX_BYTES+'{}',ROOT_SHA)['state'],'invalid')
        data=examples();data['cases']*=2
        self.assertEqual(service.inspect(json.dumps(data,ensure_ascii=False)+' '*service.MAX_BYTES,ROOT_SHA)['state'],'invalid')
        self.assertEqual(service.inspect('\ud800',ROOT_SHA)['state'],'invalid')

    def test_duplicate_keys_constants_and_deep_json_fail_closed(self):
        raw=json.dumps(examples())
        for value in [raw.replace('"schema": 1','"schema": 1, "schema": 1'),raw.replace('"schema": 1','"schema": NaN'),'['*2000+']'*2000,'{broken']:
            self.assertEqual(service.inspect(value,ROOT_SHA)['state'],'invalid')

    def test_prompt_binds_exact_files_and_version_without_expected_answer(self):
        r=inspect(examples());text=service.prompt('shared-demo','1.0.7',r,r['cases'][0])
        for value in ['shared-demo','1.0.7',ROOT_SHA,r['file_sha256'],'MCP','не отправляй','case-0']:
            self.assertIn(value,text)
        self.assertNotIn('Название сохранено',text);self.assertIn(r['cases'][0]['input'],text)

    def test_every_untrusted_field_is_escaped_in_panel_and_prompt(self):
        data=examples();data['cases'][0]['title']='</summary><script>fake()</script>'
        data['cases'][0]['input']='</textarea><img src=x onerror=fake()>'
        data['cases'][0]['expected_facts']=['<b>fact</b>'];data['cases'][0]['forbidden_actions']=['<i>action</i>']
        text=panel(inspect(data),skill_id='shared-<script>x</script>',version='<bad>',published=True)
        for value in ['<script>','<img src=x','<b>fact','<i>action','<bad>']:self.assertNotIn(value,text)
        self.assertIn('&lt;/textarea&gt;',text);self.assertIn('&lt;b&gt;fact',text)

    def test_private_card_has_criteria_but_no_mcp_prompt(self):
        r=inspect(examples());text=panel(r,skill_id='private',version='1',published=False)
        self.assertIn('Название сохранено',text);self.assertIn('закрытая сохранённая редакция',text)
        self.assertNotIn('textarea',text);self.assertNotIn('Готовый запрос',text)

    def test_invalid_input_never_echoes_raw_data_in_report(self):
        r=service.inspect('{PRIVATE-SYNTHETIC-DO-NOT-ECHO',ROOT_SHA)
        self.assertNotIn('PRIVATE-SYNTHETIC',json.dumps(r))
        self.assertNotIn('PRIVATE-SYNTHETIC',panel(r,skill_id='demo',version='1',published=True))

    def test_no_prompt_for_nonready_report_or_foreign_case(self):
        r=inspect(examples())
        for report,case in [(service.inspect(None,ROOT_SHA),r['cases'][0]),(r,{'id':'unknown'})]:
            with self.assertRaises(ValueError):service.prompt('demo','1',report,case)

    def test_inspection_does_not_mutate_author_material(self):
        data=examples();original=copy.deepcopy(data);r=inspect(data)
        self.assertEqual(data,original);self.assertEqual(r['state'],'ready')
