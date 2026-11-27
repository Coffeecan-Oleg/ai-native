import unittest

from gateway_mcp.services.text_search import SearchQuery


class SearchTermsTests(unittest.TestCase):
    def test_stop_words_cannot_turn_a_question_into_browse(self):
        self.assertEqual(SearchQuery('как у нас?').match('у нас', 'как это работает'), (0, ''))
        self.assertEqual(SearchQuery('   ').match('паспорт', 'текст'), (1, ''))

    def test_tax_ids_and_explicit_company_must_match(self):
        self.assertEqual(SearchQuery('Какой ИНН у Акметрона?').match('Паспорт', 'ИНН иной организации')[0], 0)
        self.assertEqual(SearchQuery('ИНН 12345').match('Акметрон', 'ИНН 123456')[0], 0)
        self.assertEqual(SearchQuery('Какой КПП у Акметрона?').match('Акметрон', 'ИНН 123456')[0], 0)
        self.assertGreater(SearchQuery('Реквизиты').match('Паспорт', 'ОГРН 123456')[0], 0)

    def test_inflections_yo_and_exact_identifiers(self):
        self.assertGreater(SearchQuery('Сравни предложения с требованиями').match(
            'Проверка', 'Текст предложения, требования к закупке')[0], 0)
        query = SearchQuery('отчет')
        self.assertEqual(query.match('Документы', 'Годовой отчёт')[1], 'отчёт')
        self.assertEqual(SearchQuery('подключ').match('Вход', 'О подключении'), (1, 'подключ'))
        self.assertEqual(SearchQuery('target-map-001').match('target-map-002', '001')[0], 0)
        self.assertGreater(SearchQuery('target-map-001').match('target-map-001', '')[0], 0)

    def test_specific_question_must_have_enough_evidence(self):
        self.assertEqual(SearchQuery('Сравни предложение поставщика с требованиями').match(
            'Проверка', 'Только описание поставщика')[0], 0)
