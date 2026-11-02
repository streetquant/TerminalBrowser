"""Provider-free actual activation receipt and optional failure telemetry controls."""
from __future__ import annotations
from dataclasses import replace
import errno
import json
from pathlib import Path
from unittest import mock
import pytest
import termdeck.codex_auth as auth
import termdeck.codex_credentials as credentials
import termdeck.tmux_backend as T
import termdeck.web_accounts as W
from test_web_accounts import accounts, request
SENTINEL = 'fixture-private-prompt-and-OAuth-secret-must-not-escape'
GENERIC = 'Account selection was applied, but its activation receipt could not be saved; inspect before retrying'

def safe(value):
    encoded = json.dumps(value, sort_keys=True)
    assert SENTINEL not in encoded
    assert all((secret not in encoded for secret in ('alpha-refresh', 'beta-refresh', 'gamma-refresh')))
    return encoded

def perform(fixture, monkeypatch, injection=None):
    store, rows, manager, backend = fixture
    before = {'source_current': (manager.account_source / 'current').read_bytes(), 'source_auth_link': (manager.account_source / 'auth.json').readlink(), 'credentials': {p.name: p.read_bytes() for p in (manager.account_source / 'accounts').glob('*.json')}, 'observer': (Path(rows[1].codex_home) / 'auth.json').read_bytes()}
    original = manager.record_activation
    records = []

    def record(record, state, activation):
        records.append(record.id)
        if injection:
            return injection(original, record, state, activation)
        return original(record, state, activation)
    monkeypatch.setattr(manager, 'record_activation', record)
    req = request(fixture)
    result = W.handle(store, req, manager=manager, backend=backend)
    assert backend.calls == [rows[0].id]
    assert records == [rows[0].id]
    assert manager.state(rows[0]).selected_account_key == 'beta'
    assert (manager.account_source / 'current').read_bytes() == before['source_current']
    assert (manager.account_source / 'auth.json').readlink() == before['source_auth_link']
    assert {p.name: p.read_bytes() for p in (manager.account_source / 'accounts').glob('*.json')} == before['credentials']
    assert (Path(rows[1].codex_home) / 'auth.json').read_bytes() == before['observer']
    stored = store.conn.execute('SELECT result FROM web_account_requests WHERE session_id=? AND request_id=?', (req['_actor_session'], req['request_id'])).fetchone()
    assert stored and json.loads(stored[0]) == result
    safe(result)
    return (result, req)

def assert_refusal(result, reason, code=None):
    assert result['ok'] is False
    row = result['results'][0]
    assert row['ok'] is False and row['error'] == GENERIC
    assert row['state']['auth_restart_required'] is True
    assert row['activation_receipt_failure']['reason'] == reason
    assert row['activation_receipt_failure']['errno'] == code
    assert T._project_auth_activation_receipt_failure(row['activation_receipt_failure']) == row['activation_receipt_failure']

def test_actual_stopped_receipt_success_unchanged(accounts, monkeypatch):
    result, _ = perform(accounts, monkeypatch)
    assert result['ok'] is True
    row = result['results'][0]
    assert 'activation_receipt_failure' not in row
    path = Path(accounts[1][0].codex_home) / '.termdeck-auth-runtime.json'
    receipt = json.loads(path.read_text())
    assert receipt['kind'] == 'stopped' and receipt['pid'] is None
    assert receipt['account_key'] == 'beta' and path.stat().st_mode & 511 == 384

@pytest.mark.parametrize('case,reason', [('kind', 'lifecycle-kind-unverified'), ('lifecycle-pid', 'lifecycle-process-mismatch'), ('digest', 'credential-changed'), ('process', 'process-unobservable'), ('tree', 'process-tree-unobservable'), ('start-change', 'process-tree-unobservable'), ('tree-kind', 'process-tree-changed')])
def test_actual_manager_receipt_guards(accounts, monkeypatch, case, reason):

    def inject(original, record, state, activation):
        with monkeypatch.context() as local:
            if case == 'kind':
                activation = replace(activation, credential_lifecycle='invalid')
            elif case == 'lifecycle-pid':
                activation = replace(activation, pid=123)
            elif case == 'digest':
                state = replace(state, effective_fingerprint='f' * 64)
            else:
                activation = replace(activation, pid=123, credential_lifecycle='codex-restart')
                local.setattr(credentials, 'process_start', lambda _: None if case == 'process' else 101)
                local.setattr(credentials, '_codex_processes', lambda _: None if case == 'tree' else ())
                if case == 'start-change':
                    local.setattr(credentials, 'process_start', mock.Mock(side_effect=[101, 102]))
                    local.setattr(credentials, '_codex_processes', lambda _: ((123, 101),))
            return original(record, state, activation)
    result, _ = perform(accounts, monkeypatch, inject)
    assert_refusal(result, reason)

def test_actual_writer_rejects_unsafe_receipt_path(accounts, monkeypatch):

    def inject(original, record, state, activation):
        (Path(record.codex_home) / '.termdeck-auth-runtime.json').mkdir()
        return original(record, state, activation)
    result, _ = perform(accounts, monkeypatch, inject)
    assert_refusal(result, 'receipt-path-unsafe')

def test_actual_writer_path_inspection_error(accounts, monkeypatch):

    def inject(original, record, state, activation):
        target = Path(record.codex_home) / '.termdeck-auth-runtime.json'
        original_lstat = Path.lstat

        def lstat(path, *args, **kw):
            if path == target:
                raise PermissionError(errno.EACCES, SENTINEL, str(target))
            return original_lstat(path, *args, **kw)
        with monkeypatch.context() as local:
            local.setattr(Path, 'lstat', lstat)
            return original(record, state, activation)
    result, _ = perform(accounts, monkeypatch, inject)
    assert_refusal(result, 'receipt-path-unavailable', errno.EACCES)

@pytest.mark.parametrize('fault,reason,code', [(OSError(errno.ENOSPC, SENTINEL), 'io-error', errno.ENOSPC), (OSError(errno.EIO, SENTINEL), 'io-error', errno.EIO), (RuntimeError(SENTINEL), 'unknown', None), (ValueError({'prompt': SENTINEL}), 'unknown', None)])
def test_actual_receipt_writer_faults_keep_original_outcome(accounts, monkeypatch, fault, reason, code):

    def inject(original, record, state, activation):

        def write(*_, **__):
            raise fault
        with monkeypatch.context() as local:
            local.setattr(auth, '_write_private_text', write)
            return original(record, state, activation)
    result, _ = perform(accounts, monkeypatch, inject)
    assert_refusal(result, reason, code)

@pytest.mark.parametrize('case,reason', [('unsafe', 'credential-storage-unsafe'), ('malformed', 'credential-malformed'), ('duplicate', 'credential-malformed')])
def test_actual_credential_reader_refusals(accounts, monkeypatch, case, reason):

    def inject(original, record, state, activation):
        target = (Path(record.codex_home) / 'auth.json').resolve(strict=True)
        before, mode = (target.read_bytes(), target.stat().st_mode & 511)
        try:
            if case == 'unsafe':
                target.chmod(438)
            elif case == 'duplicate':
                target.write_text('{"token":"one","token":"two"}')
            else:
                target.write_text(json.dumps({'private': SENTINEL}))
            return original(record, state, activation)
        finally:
            target.write_bytes(before)
            target.chmod(mode)
    result, _ = perform(accounts, monkeypatch, inject)
    assert_refusal(result, reason)

@pytest.mark.parametrize('boundary', ['classification', 'replace', 'web-project'])
@pytest.mark.parametrize('fault', [KeyError(SENTINEL), AttributeError(SENTINEL), MemoryError(SENTINEL)])
def test_ordinary_telemetry_fault_does_not_replace_refusal_or_publication(accounts, monkeypatch, boundary, fault):

    def fail(*_):
        raise OSError(errno.ENOSPC, SENTINEL)
    if boundary == 'classification':

        def classify(*_):
            raise fault
        monkeypatch.setattr(T, '_auth_activation_receipt_failure', classify)
    elif boundary == 'replace':
        original_replace = T.replace

        def replacer(value, **kw):
            if 'activation_receipt_failure' in kw:
                raise fault
            return original_replace(value, **kw)
        monkeypatch.setattr(T, 'replace', replacer)
    else:

        def project(*_):
            raise fault
        monkeypatch.setattr(W, '_project_auth_activation_receipt_failure', project)
    result, _ = perform(accounts, monkeypatch, lambda *args: fail())
    assert result['ok'] is False and result['results'][0]['error'] == GENERIC
    assert 'activation_receipt_failure' not in result['results'][0]

@pytest.mark.parametrize('fault', [KeyboardInterrupt(), SystemExit(71)])
def test_original_baseexception_propagates_no_retry(accounts, monkeypatch, fault):
    store, rows, manager, backend = accounts
    attempts = []

    def fail(*_):
        attempts.append(1)
        raise fault
    monkeypatch.setattr(manager, 'record_activation', fail)
    with pytest.raises(type(fault)) as caught:
        W.handle(store, request(accounts), manager=manager, backend=backend)
    assert caught.value is fault and attempts == [1] and (backend.calls == [rows[0].id])

@pytest.mark.parametrize('boundary', ['classification', 'web-project'])
def test_telemetry_baseexception_is_not_swallowed(accounts, monkeypatch, boundary):
    fault = KeyboardInterrupt()

    def fail(*_):
        raise OSError(errno.ENOSPC, SENTINEL)

    def interrupt(*_):
        raise fault
    monkeypatch.setattr(accounts[2], 'record_activation', fail)
    monkeypatch.setattr(T if boundary == 'classification' else W, '_auth_activation_receipt_failure' if boundary == 'classification' else '_project_auth_activation_receipt_failure', interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        W.handle(accounts[0], request(accounts), manager=accounts[2], backend=accounts[3])
    assert caught.value is fault and accounts[3].calls == [accounts[1][0].id]

@pytest.mark.parametrize('value', [None, True, '28', -1, 999999, {'token': SENTINEL}])
def test_invalid_errno_remains_unobserved(value):
    error = OSError(errno.EIO, SENTINEL)
    error.errno = value
    result = T._auth_activation_receipt_failure(error, T.AuthActivationResult(True, False))
    assert result['reason'] == 'io-error' and result['errno'] is None
    safe(result)

def test_builtin_exception_descriptors_do_not_call_hostile_overrides():

    class Hostile(OSError):

        @property
        def errno(self):
            raise AssertionError(SENTINEL)

        def __str__(self):
            raise AssertionError(SENTINEL)
    result = T._auth_activation_receipt_failure(Hostile(errno.EIO, SENTINEL), T.AuthActivationResult(True, False))
    assert result['errno'] == errno.EIO

    class Unknown(RuntimeError):

        @property
        def args(self):
            raise AssertionError(SENTINEL)

        def __str__(self):
            raise AssertionError(SENTINEL)
    result = T._auth_activation_receipt_failure(Unknown(SENTINEL), T.AuthActivationResult(True, False))
    assert result['reason'] == 'unknown' and result['errno'] is None
    safe(result)

@pytest.mark.parametrize('message,reason', list(T._AUTH_ACTIVATION_RECEIPT_MESSAGES.items()))
def test_exact_closed_message_projection(message, reason):
    value = T._auth_activation_receipt_failure(credentials.CredentialError(message), T.AuthActivationResult(True, False))
    assert value['reason'] == reason
    safe(value)

def valid_projection():
    return T._auth_activation_receipt_failure(OSError(errno.ENOSPC, SENTINEL), T.AuthActivationResult(True, False, pid=123, credential_lifecycle='codex-restart'))

@pytest.mark.parametrize('key,value', [('schema', SENTINEL), ('phase', SENTINEL), ('reason', SENTINEL), ('errno', True), ('errno', '28'), ('errno', 999999), ('errno', {'token': SENTINEL}), ('candidate_pid', True), ('candidate_pid', 0), ('candidate_pid', -1), ('candidate_pid', {'prompt': SENTINEL}), ('lifecycle_kind', SENTINEL)])
def test_producer_rejects_malformed_projection(key, value):
    projected = valid_projection()
    projected[key] = value
    assert T._project_auth_activation_receipt_failure(projected) is None

def test_nested_and_extra_projection_fields_refused():
    value = valid_projection()
    for malformed in ({**value, 'raw': SENTINEL}, {key: item for key, item in value.items() if key != 'phase'}):
        assert T._project_auth_activation_receipt_failure(malformed) is None
    value['reason'] = 'unknown'
    assert T._project_auth_activation_receipt_failure(value) is None


def test_success_never_projects_failure_telemetry(accounts, monkeypatch):
    def unexpected(*args):
        raise AssertionError("successful account results must not project receipt failures")
    monkeypatch.setattr(W, "_project_auth_activation_receipt_failure", unexpected)
    result, _ = perform(accounts, monkeypatch)
    assert result["ok"] is True
    assert "activation_receipt_failure" not in result["results"][0]
