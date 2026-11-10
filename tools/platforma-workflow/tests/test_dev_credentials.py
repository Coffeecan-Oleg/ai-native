import unittest
from configure_dev_bitrix import validate_fields,render_env
from release_control.core import Refused

class CredentialTests(unittest.TestCase):
    def fields(self):return {'client_id':'local.copytesting.1234','client_secret':'synthetic-test-secret-not-a-real-key','member_id':''}
    def test_credentials_cannot_include_newlines_or_other_fields(self):
        for update in [{'client_secret':'fake\nGATEWAY_AUTH_ENABLED=false'},{'client_id':'http://foreign'},{'extra':'x'},{'member_id':'foreign'}]:
            fields=self.fields();fields.update(update)
            with self.subTest(update=update),self.assertRaises(Refused):validate_fields(fields)
    def test_setup_preserves_database_jwt_and_portal(self):
        old='GATEWAY_DATABASE_URL=private-db\nGATEWAY_JWT_SECRET=old-private-key\nGATEWAY_AUTH_ENABLED=true\nGATEWAY_BITRIX_PORTAL=akmetron.bitrix24.ru\nGATEWAY_BITRIX_EMBED_ENABLED=false\n'
        result=render_env(old,self.fields())
        for expected in ['GATEWAY_DATABASE_URL=private-db','GATEWAY_JWT_SECRET=old-private-key','GATEWAY_AUTH_ENABLED=true','GATEWAY_BITRIX_PORTAL=akmetron.bitrix24.ru']:
            self.assertIn(expected,result)
        self.assertEqual(result.count('GATEWAY_BITRIX_EMBED_ENABLED='),1)
        self.assertIn('GATEWAY_BITRIX_EMBED_ENABLED=true',result)
    def test_setup_does_not_add_business_service_credentials(self):
        result=render_env('GATEWAY_AUTH_ENABLED=true\n',self.fields())
        self.assertNotIn('UNISENDER',result);self.assertNotIn('YANDEX',result)

if __name__=='__main__':unittest.main()
