"""Real disposable PostgreSQL projections; no portal calls or token reads."""
import json
import os
from unittest.mock import patch

from test_reader_onboarding import ReaderFixture
from gateway_mcp.services.storage_core import _connect
from gateway_mcp.services.storage_admin_console import admin_audit_user_names

MEMBER = 'a' * 32
OTHER_MEMBER = 'b' * 32


class AuditNameProjectionTests(ReaderFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'GATEWAY_BITRIX_MEMBER_ID': MEMBER}))
        self.enterContext(patch('gateway_mcp.services.corporate_access.enabled', return_value=True))
        self.subjects = []
        self.addCleanup(self.clean_synthetic_rows)

    def clean_synthetic_rows(self):
        if self.subjects:
            with _connect() as conn:
                conn.execute('delete from audit_events where actor_subject = any(%s)', (self.subjects,))
                conn.execute('delete from user_oauth_tokens where actor_subject = any(%s)', (self.subjects,))

    def user(self,number,name,*,member=MEMBER,portal='akmetron.bitrix24.ru',
             success=True,metadata_user_id=None,revoked=False):
        subject='bitrix24:'+member+':'+str(number)
        self.subjects.append(subject)
        metadata={'portal':portal,'member_id':member,
                  'user_id':str(number if metadata_user_id is None else metadata_user_id)}
        with _connect() as conn:
            # Deliberately not decryptable: the projection must use only login.
            conn.execute('''insert into user_oauth_tokens
                (provider,actor_subject,login,access_token_encrypted,metadata,revoked_at)
                values ('bitrix24-login',%s,%s,'not-a-real-encrypted-token',%s::jsonb,
                        case when %s then now() else null end)''',
                (subject,name,json.dumps(metadata),revoked))
            if success:
                conn.execute('''insert into audit_events
                    (ts_ms,event,actor_subject,system,decision,status,payload)
                    values (1,'corporate_login',%s,'identity','allow','ok',
                            '{"arguments":{"channel":"web"}}'::jsonb)''', (subject,))
        return subject

    def test_real_projection_uses_only_successful_current_portal_profiles(self):
        own=self.user(741001,'Сергей Смирнов')
        unsigned=self.user(741002,'Нет успешного входа',success=False)
        foreign=self.user(741003,'Другой портал',member=OTHER_MEMBER)
        self.assertEqual(admin_audit_user_names([own,unsigned,foreign]), {own:'Сергей Смирнов'})

    def test_profile_metadata_and_subject_must_match_exactly(self):
        wrong_portal=self.user(741004,'Иной домен',portal='other.bitrix24.ru')
        wrong_identity=self.user(741005,'Подменённый ID',metadata_user_id=999)
        self.assertEqual(admin_audit_user_names([wrong_portal,wrong_identity]), {})

    def test_historic_revoked_identity_keeps_display_label_without_restoring_access(self):
        subject=self.user(741006,'Анна Иванова',revoked=True)
        self.assertEqual(admin_audit_user_names([subject]), {subject:'Анна Иванова'})
        with _connect() as conn:
            row=conn.execute('select revoked_at from user_oauth_tokens where actor_subject=%s',
                             (subject,)).fetchone()
        self.assertIsNotNone(row['revoked_at'])
