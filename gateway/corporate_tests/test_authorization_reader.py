"""Read-session invariants, independent of a database or optional dependencies."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import time

spec=importlib.util.spec_from_file_location('scope_reader_test',Path(__file__).parents[1]/'gateway_mcp/services/authorization_reader.py')
reader=importlib.util.module_from_spec(spec)
with patch.dict(sys.modules,{'psycopg':SimpleNamespace(connect=Mock()),'psycopg.rows':SimpleNamespace(dict_row=object())}):spec.loader.exec_module(reader)

class Connection:
    def __init__(self):self.closed=False;self.closes=0;self.queries=[];self.values={};self.failure=None;self.active=False
    def close(self):self.closed=True;self.closes+=1
    def cursor(self):
        conn=self
        class Cursor:
            def __enter__(self):
                if conn.active:raise RuntimeError('shared_transaction_overlap')
                conn.active=True;return self
            def __exit__(self,*args):conn.active=False
            def execute(self,sql,params):
                conn.queries.append((sql,params))
                if conn.failure:raise conn.failure
                time.sleep(.001);self.key=params[0]
            def fetchall(self):return [{'value':conn.values.get(self.key,self.key)}]
        return Cursor()

class ReadSessionTests(unittest.TestCase):
    def setUp(self):
        self.session=reader.ReadSession();self.connections=[]
        def connect(*args,**kwargs):
            conn=Connection();self.connections.append(conn);return conn
        self.factory=self.enterContext(patch.object(reader.psycopg,'connect',side_effect=connect))
        self.addCleanup(self.session.close)

    def fetch(self,key=1):return self.session.fetch('synthetic-dsn','select value where key=%s',[key])

    def test_connection_reused_but_every_decision_is_queried(self):
        self.assertEqual(self.fetch()[0]['value'],1)
        self.connections[0].values[1]='new-deny'
        self.assertEqual(self.fetch()[0]['value'],'new-deny')
        self.assertEqual(self.factory.call_count,1);self.assertEqual(len(self.connections[0].queries),2)

    def test_subject_parameters_and_results_do_not_cross(self):
        self.assertEqual(self.fetch('employee-a'),[{'value':'employee-a'}])
        self.assertEqual(self.fetch('employee-b'),[{'value':'employee-b'}])
        self.assertEqual(self.connections[0].queries[-1][1],['employee-b'])

    def test_connection_is_readonly_autocommit_and_bounded(self):
        self.fetch();kwargs=self.factory.call_args.kwargs
        self.assertTrue(kwargs['autocommit']);self.assertEqual(kwargs['connect_timeout'],3)
        self.assertIn('default_transaction_read_only=on',kwargs['options'])
        self.assertIn('statement_timeout=2000',kwargs['options']);self.assertIn('lock_timeout=1000',kwargs['options'])
        self.assertIsNone(kwargs['prepare_threshold'])

    def test_session_is_replaced_at_maximum_age(self):
        with patch.object(reader.time,'monotonic',return_value=100):self.fetch()
        old=self.connections[0]
        with patch.object(reader.time,'monotonic',return_value=160):self.fetch()
        self.assertTrue(old.closed);self.assertEqual(self.factory.call_count,2)

    def test_dsn_rotation_cannot_reuse_old_session(self):
        self.fetch();old=self.connections[0]
        self.session.fetch('new-synthetic-dsn','select value',[2])
        self.assertTrue(old.closed);self.assertEqual(self.factory.call_args.args,('new-synthetic-dsn',))

    def test_closed_session_is_replaced(self):
        self.fetch();self.connections[0].closed=True;self.fetch();self.assertEqual(self.factory.call_count,2)

    def test_failed_query_discards_and_does_not_retry_or_return_previous_rows(self):
        self.fetch();old=self.connections[0];old.failure=RuntimeError('private-database-details')
        with self.assertRaisesRegex(RuntimeError,'^scope_reader_unavailable$'):self.fetch()
        self.assertTrue(old.closed);self.assertIsNone(self.session.connection);self.assertEqual(self.factory.call_count,1)
        self.assertEqual(self.fetch(2),[{'value':2}]);self.assertEqual(self.factory.call_count,2)

    def test_failed_connect_is_closed_and_fail_closed(self):
        self.factory.side_effect=RuntimeError('private-credential')
        with self.assertRaisesRegex(RuntimeError,'^scope_reader_unavailable$'):self.fetch()
        self.assertIsNone(self.session.connection);self.assertEqual(self.factory.call_count,1)

    def test_busy_reader_has_a_bounded_wait_and_no_fallback(self):
        lock=Mock();lock.acquire.return_value=False;self.session.lock=lock
        with self.assertRaisesRegex(RuntimeError,'scope_reader_busy'):self.fetch()
        lock.acquire.assert_called_once_with(timeout=2);self.factory.assert_not_called();lock.release.assert_not_called()
        self.session.lock=reader.Lock()

    def test_concurrent_readers_cannot_share_an_active_transaction(self):
        with ThreadPoolExecutor(max_workers=8) as workers:values=list(workers.map(self.fetch,range(40)))
        self.assertEqual(values,[[{'value':i}] for i in range(40)])
        self.assertEqual(self.factory.call_count,1);self.assertFalse(self.connections[0].active)

    def test_foreign_process_never_uses_or_closes_inherited_connection(self):
        self.fetch();old=self.connections[0]
        with patch.object(reader.os,'getpid',return_value=self.session.pid+1):
            with self.assertRaisesRegex(RuntimeError,'process_mismatch'):self.fetch()
            self.session.close()
        self.assertFalse(old.closed);self.assertEqual(len(old.queries),1)

    def test_fork_callback_replaces_lock_without_closing_parent_session(self):
        previous=reader._session
        try:
            reader._session=self.session;self.fetch();old=self.connections[0]
            reader._after_fork()
            self.assertIsNot(reader._session.lock,self.session.lock);self.assertIsNone(reader._session.connection)
            self.assertFalse(old.closed)
        finally:reader._session=previous

    def test_cleanup_releases_session(self):
        self.fetch();old=self.connections[0];self.session.close();self.session.close()
        self.assertTrue(old.closed);self.assertIsNone(self.session.connection);self.assertEqual(old.closes,1)

if __name__=='__main__':unittest.main()
