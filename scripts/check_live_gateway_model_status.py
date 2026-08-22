#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from termdeck.gateway_models import record_uses_gateway_models
from termdeck.models import SessionRecord
from termdeck.tmux_backend import TmuxBackend


def _record_from_legacy_row(row: sqlite3.Row, columns: set[str]) -> SessionRecord:
    def value(name: str, default=None):
        return row[name] if name in columns else default

    return SessionRecord(
        id=value("id"),
        name=value("name"),
        tag=value("tag"),
        command=value("command"),
        cwd=value("cwd"),
        tmux_name=value("tmux_name"),
        created_at=value("created_at"),
        last_opened_at=value("last_opened_at"),
        status=value("status"),
        last_pid=value("last_pid"),
        window_id=value("window_id", "main"),
        window_title=value("window_title", "Main"),
        tab_index=int(value("tab_index", 0) or 0),
        visible=bool(value("visible", 1)),
        pinned=bool(value("pinned", 0)),
        pane_target=value("pane_target"),
        external=bool(value("external", 0)),
        recovery_command=value("recovery_command"),
        recovery_cwd=value("recovery_cwd"),
        auth_scope_id=value("auth_scope_id"),
        codex_home=value("codex_home"),
        selected_account=value("selected_account"),
        pending_account=value("pending_account"),
        auth_restart_required=bool(value("auth_restart_required", 0)),
        auth_changed_at=value("auth_changed_at"),
        recovery_recipe=value("recovery_recipe"),
        recovery_recipe_sha256=value("recovery_recipe_sha256"),
        restart_policy=value("restart_policy", "on-failure") or "on-failure",
        recovery_attempts=int(value("recovery_attempts", 0) or 0),
        recovery_last_attempt_at=value("recovery_last_attempt_at"),
        recovery_last_error=value("recovery_last_error"),
    )


def inspect(database: Path, *, sample_limit: int) -> dict[str, object]:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")
        }
        rows = connection.execute(
            "SELECT * FROM sessions ORDER BY window_id, tab_index"
        ).fetchall()
    finally:
        connection.close()

    backend = TmuxBackend(query_timeout=1.0, command_timeout=3.0)
    results: list[dict[str, object]] = []
    for row in rows:
        record = _record_from_legacy_row(row, columns)
        if not record_uses_gateway_models(record):
            continue
        try:
            live = backend.session_exists(record)
            pid = backend.pane_pid(record) if live else None
            status = backend.gateway_model_status(record)
            results.append(
                {
                    "id": record.id,
                    "name": record.name,
                    "registry_status": record.status,
                    "live": live,
                    "pid": pid,
                    "available": status.available,
                    "stale": status.stale,
                    "model_count": status.model_count,
                    "fingerprint": status.fingerprint,
                    "detail": status.detail,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": record.id,
                    "name": record.name,
                    "registry_status": record.status,
                    "error": str(exc),
                }
            )

    summary: dict[str, object] = {
        "database": str(database),
        "schema_has_recovery_recipe": "recovery_recipe" in columns,
        "count": len(results),
        "live": sum(bool(row.get("live")) for row in results),
        "stale": sum(bool(row.get("stale")) for row in results),
        "fresh": sum(
            bool(row.get("live"))
            and bool(row.get("available"))
            and not bool(row.get("stale"))
            for row in results
        ),
        "unavailable": sum(
            "error" in row or not bool(row.get("available")) for row in results
        ),
        "model_counts": sorted(
            {int(row["model_count"]) for row in results if row.get("model_count")}
        ),
        "fingerprints": sorted(
            {str(row["fingerprint"]) for row in results if row.get("fingerprint")}
        ),
        "sample": results[: max(0, sample_limit)],
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".local" / "share" / "termdeck" / "sessions.sqlite3",
    )
    parser.add_argument("--sample-limit", type=int, default=10)
    args = parser.parse_args()
    print(
        json.dumps(
            inspect(args.db.expanduser().resolve(), sample_limit=args.sample_limit),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
