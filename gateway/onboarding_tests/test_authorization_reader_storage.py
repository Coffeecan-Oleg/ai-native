"""Fresh authorization decisions on a disposable loopback PostgreSQL only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import select
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from gateway_mcp.services import authorization_reader as reader
from gateway_mcp.services import auth,bitrix_identity as identity,corporate_access as corporate
from gateway_mcp.services import storage,storage_core

class AuthorizationStorageTests(unittest.TestCase):
    def setUp(self):
        self.url=os.environ.get('ONBOARDING_TEST_DATABASE_URL','')
        if not self.url:self.skipTest('Disposable PostgreSQL required')
        parsed=urlsplit(self.url)
        if parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.path!='/platforma_onboarding_test':
            raise RuntimeError('disposable_database_required')
        self.uid=str(100000+uuid4().int%1000000000);self.other=str(int(self.uid)+1)
        self.subject='bitrix24:'+'a'*32+':'+self.uid;self.jti='synthetic-'+uuid4().hex
        self.enterContext(patch.dict(os.environ,{
            'GATEWAY_DATABASE_URL':self.url,'GATEWAY_LOGIN_PROVIDER':'bitrix24','GATEWAY_BITRIX_PORTAL':'akmetron.bitrix24.ru',
            'GATEWAY_BITRIX_CLIENT_ID':'local.synthetic.12345678','GATEWAY_BITRIX_CLIENT_SECRET':'synthetic-not-real',
            'GATEWAY_BITRIX_MEMBER_ID':'a'*32,'GATEWAY_BITRIX_OWNER_USER_ID':'20','GATEWAY_BITRIX_DENIED_USER_IDS':'',
            'GATEWAY_BITRIX_UNISENDER_USER_IDS':self.uid+','+self.other,'GATEWAY_AUTH_ENABLED':'true',
            'GATEWAY_JWT_SECRET':'synthetic-readonly-test-'+'x'*48,
            'GATEWAY_ISSUER_URL':'https://authorization-test.invalid','GATEWAY_RESOURCE_URL':'https://authorization-test.invalid/mcp'}))
        self.session=reader.ReadSession();self.enterContext(patch.object(reader,'_session',self.session))
        self.enterContext(patch.object(identity,'current_employee',side_effect=lambda subject:{'user_id':identity.parse_subject(subject)[1],'name':'Synthetic','verified_at':time.time()}))
        storage_core.ensure_schema()
        self.addCleanup(self.clean_rows);self.addCleanup(self.session.close)

    def clean_rows(self):
        with psycopg.connect(self.url) as conn:
            conn.execute('delete from access_scope_grants where subject_type=%s and subject_key=%s',('user',self.subject))
            conn.execute('delete from gateway_oauth_revoked_access_tokens where jti=%s',(self.jti,))

    def deny(self):
        with psycopg.connect(self.url) as conn:
            return conn.execute("insert into access_scope_grants(subject_type,subject_key,scope,effect) values ('user',%s,'unisender:read','deny') returning id",(self.subject,)).fetchone()[0]

    def pid(self):return reader.fetch(self.url,'select pg_backend_pid() as pid',()) [0]['pid']

    def test_same_connection_sees_new_deny_and_its_revocation(self):
        self.assertIn('unisender:read',corporate.fresh_actor(self.subject).scopes);pid=self.pid();grant=self.deny()
        self.assertNotIn('unisender:read',corporate.fresh_actor(self.subject).scopes)
        with psycopg.connect(self.url) as conn:conn.execute('update access_scope_grants set revoked_at=clock_timestamp() where id=%s',(grant,))
        self.assertIn('unisender:read',corporate.fresh_actor(self.subject).scopes);self.assertEqual(self.pid(),pid)

    def test_grant_expiration_is_not_cached(self):
        grant=self.deny();self.assertNotIn('unisender:read',corporate.fresh_actor(self.subject).scopes)
        with psycopg.connect(self.url) as conn:conn.execute("update access_scope_grants set expires_at=clock_timestamp()-interval '1 second' where id=%s",(grant,))
        self.assertIn('unisender:read',corporate.fresh_actor(self.subject).scopes)

    def test_new_jwt_revocation_is_seen_without_reconnecting(self):
        self.assertFalse(storage.gateway_oauth_access_token_revoked(self.jti));pid=self.pid()
        with psycopg.connect(self.url) as conn:conn.execute("insert into gateway_oauth_revoked_access_tokens(jti,expires_at) values (%s,clock_timestamp()+interval '1 minute')",(self.jti,))
        self.assertTrue(storage.gateway_oauth_access_token_revoked(self.jti));self.assertEqual(self.pid(),pid)
        with psycopg.connect(self.url) as conn:conn.execute("update gateway_oauth_revoked_access_tokens set expires_at=clock_timestamp()-interval '1 second' where jti=%s",(self.jti,))
        self.assertFalse(storage.gateway_oauth_access_token_revoked(self.jti))

    def test_a_signed_revoked_token_fails_verification(self):
        token=auth.mint_gateway_token(corporate.delegated(corporate.fresh_actor(self.subject)),ttl_seconds=60)
        claims=auth.verify_gateway_token_claims(token);self.jti=claims['jti']
        storage.revoke_gateway_oauth_access_token(jti=self.jti,expires_at_epoch=claims['exp'])
        with self.assertRaises(PermissionError):auth.verify_gateway_token_claims(token)
        self.assertIsNone(asyncio.run(auth.GatewayJwtVerifier().verify_token(token)))

    def test_parallel_subjects_keep_fresh_independent_decisions(self):
        self.deny();other_subject='bitrix24:'+'a'*32+':'+self.other
        inputs=[self.subject,other_subject]*12
        with ThreadPoolExecutor(max_workers=8) as pool:
            values=list(pool.map(lambda sub:'unisender:read' in corporate.fresh_actor(sub).scopes,inputs))
        self.assertEqual(values,[False,True]*12)
        self.assertTrue(self.session.connection.autocommit)
        self.assertEqual(self.session.connection.info.transaction_status,psycopg.pq.TransactionStatus.IDLE)

    def test_reader_connection_cannot_write(self):
        self.pid()
        with self.assertRaisesRegex(RuntimeError,'^scope_reader_unavailable$'):
            reader.fetch(self.url,"insert into access_scope_grants(subject_type,subject_key,scope) values ('user',%s,'forbidden:write')",(self.subject,))
        with psycopg.connect(self.url) as conn:self.assertEqual(conn.execute('select count(*) from access_scope_grants where subject_key=%s',(self.subject,)).fetchone()[0],0)
        self.assertIsNone(self.session.connection)

    def test_connection_failure_cannot_use_previous_allow(self):
        self.assertIn('unisender:read',corporate.fresh_actor(self.subject).scopes)
        self.session.connection.close()
        with patch.object(reader.psycopg,'connect',side_effect=RuntimeError('private-credential-never-output')):
            with self.assertRaisesRegex(RuntimeError,'^scope_reader_unavailable$'):corporate.fresh_actor(self.subject)
        self.assertIsNone(self.session.connection)

    def test_table_lock_is_bounded_and_recovers(self):
        self.pid();started=time.monotonic()
        with psycopg.connect(self.url) as conn:
            conn.execute('lock table access_scope_grants in access exclusive mode')
            with self.assertRaisesRegex(RuntimeError,'^scope_reader_unavailable$'):corporate.fresh_actor(self.subject)
        self.assertLess(time.monotonic()-started,5)
        self.assertIn('unisender:read',corporate.fresh_actor(self.subject).scopes)

    def test_fork_gets_new_session_and_parent_remains_usable(self):
        if not hasattr(os,'fork'):self.skipTest('Linux fork check')
        parent_pid=self.pid();readfd,writefd=os.pipe();child=os.fork()
        if child==0:
            try:
                os.close(readfd);value=reader.fetch(self.url,'select pg_backend_pid() as pid',())[0]['pid']
                os.write(writefd,json.dumps({'pid':value}).encode());os._exit(0)
            except BaseException:os._exit(1)
        os.close(writefd)
        try:
            ready,_,_=select.select([readfd],[],[],8)
            if not ready:
                os.kill(child,9);os.waitpid(child,0);self.fail('forked reader timed out')
            data=os.read(readfd,1024);_,status=os.waitpid(child,0)
            self.assertEqual(status,0);self.assertNotEqual(json.loads(data)['pid'],parent_pid)
            self.assertEqual(self.pid(),parent_pid)
        finally:os.close(readfd)

if __name__=='__main__':unittest.main()
