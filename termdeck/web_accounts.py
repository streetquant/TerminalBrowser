"""Authenticated management bridge for TermDeck's existing account lifecycle."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .codex_auth import CodexAuthManager, CodexAuthError, resolve_account
from .tmux_backend import (TmuxBackend, auth_transaction_lock, execute_auth_change,
    _project_auth_activation_receipt_failure)
from .session_store import utc_now


def _eligible(store):
    return [row for row in store.list(include_archived=False)
            if row.auth_scope_id and row.codex_home and not row.external]


def _row(manager, record):
    state = manager.state(record)
    result = state.to_dict()
    result.pop("codex_home", None)
    result.update(manager.runtime_state(record, state))
    result.update(id=record.id, name=record.name, window=record.window_title,
                  recorded_account=record.selected_account, pending_account=record.pending_account,
                  auth_restart_required=record.auth_restart_required)
    return result


def _scope_source(manager, record):
    from .codex_config_scope import _bound_canonical_home
    if _bound_canonical_home(Path(record.codex_home)) != manager.account_source.resolve(strict=True):
        raise CodexAuthError("Private scope is bound to a different account source; unchanged")


def handle(store, request, *, manager=None, backend=None):
    from .web_management import ManagementError
    operation = request["operation"]
    if manager is None:
        from .agent_launch import policy
        value, _ = policy(store.path.parent)
        if not value["codex"]:
            raise ManagementError("TermDeck account source is not configured in host launch policy")
        manager = CodexAuthManager(account_source=value["codex"]["account_source"])
    records = _eligible(store)
    if operation == "accounts":
        rows = []
        for record in records:
            try: rows.append(_row(manager, record))
            except (CodexAuthError, OSError, ValueError):
                rows.append({"id": record.id, "name": record.name, "window": record.window_title,
                             "error": "Private account state is unavailable; preserved for review", "accounts": []})
        return {"ok": True, "rows": rows, "scope": [row.id for row in records]}
    session, request_id = request.get("_actor_session"), request.get("request_id")
    if not isinstance(session, str) or not session or not isinstance(request_id, str) or not re.fullmatch("[a-f0-9]{32}", request_id):
        raise ManagementError("Authenticated account request identity required")
    ids, expected, account = request.get("ids"), request.get("expected"), request.get("account")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 128 or not all(isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
        raise ManagementError("Select exact managed tab identities")
    if not isinstance(expected, dict) or set(expected) != set(ids) or not all(isinstance(v, str) and len(v) <= 256 for v in expected.values()):
        raise ManagementError("Expected account states required")
    if not isinstance(account, str) or not 1 <= len(account) <= 128:
        raise ManagementError("Invalid account selector")
    if request.get("confirmation") != ids:
        raise ManagementError("Confirm the exact account-switch scope")
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    store.conn.execute("CREATE TABLE IF NOT EXISTS web_account_requests(session_id TEXT,request_id TEXT,digest TEXT,result TEXT,PRIMARY KEY(session_id,request_id))")
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        prior = store.conn.execute("SELECT digest,result FROM web_account_requests WHERE session_id=? AND request_id=?", (session, request_id)).fetchone()
        if prior:
            if prior[0] != digest:
                raise ManagementError("Account request identity already used with different values")
            return json.loads(prior[1]) if prior[1] else {"ok": False, "error": "Account transaction pending or interrupted; inspect current state before a new action"}
        if operation == "switch-account" and len(ids) != 1:
            raise ManagementError("A per-tab switch requires exactly one tab")
        if operation == "switch-all-accounts" and set(ids) != {row.id for row in records}:
            raise ManagementError("All-tab scope changed; refresh and confirm again")
        if not set(ids) <= {row.id for row in records}:
            raise ManagementError("Selected tab is no longer available for account switching")
        if store.conn.execute("SELECT count(*) FROM web_account_requests").fetchone()[0] >= 32768:
            raise ManagementError("Account request receipt capacity reached")
        store.conn.execute("INSERT INTO web_account_requests VALUES(?,?,?,NULL)", (session, request_id, digest))
    results = []
    backend = backend or TmuxBackend()
    for session_id in ids:
        record = store.get(session_id)
        diagnostics_enabled = False
        if isinstance(backend, TmuxBackend):
            try:
                backend.take_auth_readiness_diagnostics(session_id)
                diagnostics_enabled = True
            except Exception:
                # A failed clear has no account authority and cannot attach stale evidence.
                pass
        try:
            if record is None:
                raise ManagementError("Selected tab disappeared before account switching")
            _scope_source(manager, record)
            with auth_transaction_lock(record):
                current = store.get(session_id)
                if current is None or current.external or current.codex_home != record.codex_home or current.auth_scope_id != record.auth_scope_id:
                    raise ManagementError("Private scope changed before account switching")
                record = current
                state = manager.state(record)
                if state.fingerprint != expected[session_id]:
                    raise ManagementError("Account state changed; refresh before switching")
                resolve_account(state, account)
                transaction = execute_auth_change(backend, manager, record, state, account)
                if transaction.switch_result is not None:
                    from .cli import _auth_activation_updates
                    state = transaction.switch_result.state
                    changes = dict(selected_account=state.selected_account, pending_account=None,
                                   auth_restart_required=not bool(transaction.activation and transaction.activation.ok), auth_changed_at=utc_now())
                    if transaction.activation:
                        changes.update(_auth_activation_updates(transaction.activation))
                    record = store.update(record.id, **changes)
                results.append({"id": session_id, "ok": transaction.ok, "error": transaction.error,
                                "restarted": bool(transaction.activation and transaction.activation.restarted),
                                "state": _row(manager, record)})
                if not transaction.ok:
                    try:
                        receipt_failure = _project_auth_activation_receipt_failure(
                            transaction.activation_receipt_failure)
                        if receipt_failure is not None:
                            results[-1]["activation_receipt_failure"] = receipt_failure
                    except Exception:
                        # Optional receipt telemetry cannot affect the account result.
                        pass
        except (CodexAuthError, OSError, ValueError, RuntimeError) as exc:
            # No exception repr/credential payload reaches the API.
            from .redaction import redact_text
            results.append({"id": session_id, "ok": False, "error": redact_text(str(exc)).text[:512]})
        if diagnostics_enabled:
            try:
                diagnostics = backend.take_auth_readiness_diagnostics(session_id)
                if diagnostics:
                    results[-1]["readiness_diagnostics"] = diagnostics
            except Exception:
                # Telemetry cannot replace the account result or its receipt publication.
                pass
    result = {"ok": all(row["ok"] for row in results), "results": results}
    with store.conn:
        store.conn.execute("UPDATE web_account_requests SET result=? WHERE session_id=? AND request_id=?", (json.dumps(result), session, request_id))
    return result
