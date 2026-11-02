"""Competing SQLite writers reproduce concurrent native preparation."""
from contextlib import contextmanager
import sqlite3
import threading
import time
import pytest
from termdeck import agent_launch as launch
from termdeck import client_files as cf
from test_agent_launch import configured, Backend, request
from test_client_files import stage, ordinary_workspace_root

@contextmanager
def held_machine(store, *, revoke=False, delay=2.3):
    ready, finish = threading.Event(), threading.Event()
    failures = []
    def writer():
        try:
            with sqlite3.connect(store.auth_directory / 'termdeck-machines-v1.sqlite3') as db:
                db.execute('BEGIN IMMEDIATE')
                if revoke: db.execute('UPDATE machines SET revoked_at_ms=1')
                ready.set()
                finish.wait(delay)
                db.commit()
        except BaseException as error:
            failures.append(error); ready.set()
    thread = threading.Thread(target=writer); thread.start()
    assert ready.wait(5) and not failures
    try: yield
    finally:
        finish.set(); thread.join(5)
        assert not thread.is_alive() and not failures

@pytest.mark.parametrize('revoke', [False, True])
def test_waits_past_old_budget_and_revalidates(stage, revoke):
    store, actor = stage; started = time.monotonic()
    with held_machine(store, revoke=revoke):
        if revoke:
            with pytest.raises(cf.StagingError, match='authorization failed'):
                with cf.authorized(store.auth_directory, actor, lock_timeout_ms=30000):
                    pytest.fail('Queued launch admitted revoked machine')
        else:
            with cf.authorized(store.auth_directory, actor, lock_timeout_ms=30000):
                assert time.monotonic() - started >= 2.1
    assert time.monotonic() - started < 8

def test_busy_budget_never_yields_authority(stage):
    store, actor = stage
    with held_machine(store, delay=10):
        started = time.monotonic()
        with pytest.raises(cf.StagingError, match='authenticated operation is busy') as caught:
            with cf.authorized(store.auth_directory, actor, lock_timeout_ms=100):
                pytest.fail('Busy authorization yielded')
        assert caught.value.code == 'busy'
        assert 0.07 <= time.monotonic() - started < 1.5

def test_execution_waits_before_consuming_launch(configured, monkeypatch):
    store, actor, _, _ = configured
    receipt = store.handle(actor, request(configured)); backend = Backend()
    launch.start(store, actor, receipt, backend=backend)
    args = launch._launch_binding(backend.records[0]); seen = []
    @contextmanager
    def admission(directory, value, **kwargs):
        seen.append(kwargs)
        raise cf.StagingError('fixture busy', 'busy')
        yield
    monkeypatch.setattr(launch, 'authorized', admission)
    with pytest.raises(cf.StagingError, match='fixture busy'): launch.execute(args)
    assert seen == [{'lock_timeout_ms': 30000}]
    tree = cf.Directory(store.root)
    try:
        with cf._database(tree, 'metadata.sqlite3', readonly=True) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='launch_runs'").fetchone()
            if exists: assert not db.execute('SELECT 1 FROM launch_runs WHERE launch_id=?', (receipt['launch_id'],)).fetchone()
    finally: tree.close()

def test_resume_busy_keeps_busy_classification(stage, monkeypatch):
    store, actor = stage
    @contextmanager
    def busy(*args, **kwargs):
        raise cf.StagingError('fixture busy', 'busy')
        yield
    monkeypatch.setattr(launch, 'authorized', busy)
    with pytest.raises(cf.StagingError, match='fixture busy') as caught:
        with launch._resume_authorized(store, actor, lock_timeout_ms=30000): pytest.fail('Busy resume admitted')
    assert caught.value.code == 'busy'
