"""Bounded process-local authorization reads; never caches permissions or revocations."""
import atexit
import os
from threading import Lock
import time

import psycopg
from psycopg.rows import dict_row

WAIT_SECONDS=2
MAX_AGE_SECONDS=60


class ReadSession:
    def __init__(self):
        self.pid=os.getpid()
        self.lock=Lock()
        self.connection=None
        self.dsn=None
        self.created=0

    def _discard(self):
        conn,self.connection=self.connection,None
        self.dsn=None
        if conn is not None:
            try:conn.close()
            except Exception:pass

    def fetch(self,dsn,sql,params):
        if self.pid!=os.getpid():raise RuntimeError('scope_reader_process_mismatch')
        if not self.lock.acquire(timeout=WAIT_SECONDS):raise RuntimeError('scope_reader_busy')
        try:
            if self.connection is not None and (self.dsn!=dsn or self.connection.closed or
                    time.monotonic()-self.created>=MAX_AGE_SECONDS):
                self._discard()
            if self.connection is None:
                self.connection=psycopg.connect(dsn,row_factory=dict_row,autocommit=True,
                    connect_timeout=3,prepare_threshold=None,
                    options='-c default_transaction_read_only=on -c statement_timeout=2000 -c lock_timeout=1000')
                self.dsn=dsn;self.created=time.monotonic()
            # Each SELECT has a fresh autocommit transaction. No rows are retained.
            with self.connection.cursor() as cur:
                cur.execute(sql,params)
                return cur.fetchall()
        except BaseException as exc:
            # Never return a stale permission or retry a failed decision silently.
            self._discard()
            if isinstance(exc,Exception):raise RuntimeError('scope_reader_unavailable') from None
            raise
        finally:self.lock.release()

    def close(self):
        if self.pid==os.getpid() and self.lock.acquire(timeout=WAIT_SECONDS):
            try:self._discard()
            finally:self.lock.release()


_session=ReadSession()

def _after_fork():
    global _session
    # Do not close/use an inherited libpq connection or inherited thread lock.
    _session=ReadSession()

if hasattr(os,'register_at_fork'):os.register_at_fork(after_in_child=_after_fork)
atexit.register(lambda:_session.close())

def fetch(dsn,sql,params):
    return _session.fetch(dsn,sql,params)
