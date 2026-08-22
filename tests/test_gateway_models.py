from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from termdeck.gateway_models import (
    GatewayModelError,
    gateway_model_status,
    load_gateway_model_inventory,
    process_start_epoch,
    record_uses_gateway_models,
)


def _record(command: str = "cc-go-ulw", recovery: str | None = None):
    return SimpleNamespace(
        command=command,
        recovery_command=recovery,
        external=False,
    )


def _write_cache(path: Path, ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "baseUrl": "http://127.0.0.1:8318",
                "fetchedAt": "2026-08-22T00:04:01Z",
                "models": [{"id": model_id, "displayName": model_id} for model_id in ids],
            }
        ),
        encoding="utf-8",
    )


def test_inventory_is_canonical_order_independent_and_deduplicated(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_cache(first, ["claude-z", "claude-a", "claude-a"])
    _write_cache(second, ["claude-a", "claude-z"])

    left = load_gateway_model_inventory(first)
    right = load_gateway_model_inventory(second)

    assert left.model_ids == ("claude-a", "claude-z")
    assert left.count == 2
    assert left.fingerprint == right.fingerprint
    assert left.base_url == "http://127.0.0.1:8318"



def test_inventory_accepts_upstream_epoch_millisecond_fetched_at(tmp_path: Path) -> None:
    cache = tmp_path / "models.json"
    cache.write_text(
        json.dumps(
            {
                "baseUrl": "http://127.0.0.1:8318",
                "fetchedAt": 1787337255780,
                "models": [{"id": "claude-a"}],
            }
        ),
        encoding="utf-8",
    )
    inventory = load_gateway_model_inventory(cache)
    assert inventory.fetched_at == 1787337255780
    assert inventory.count == 1

def test_inventory_rejects_symlink_and_malformed_rows(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    _write_cache(real, ["claude-a"])
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(GatewayModelError, match="regular file"):
        load_gateway_model_inventory(link)

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"baseUrl":"x","models":[{"id":""}]}', encoding="utf-8")
    with pytest.raises(GatewayModelError, match="model id"):
        load_gateway_model_inventory(malformed)


def test_exact_cc_go_ulw_sessions_are_detected_without_matching_unrelated_commands() -> None:
    assert record_uses_gateway_models(
        _record(recovery="cc-go-ulw --resume d2a7ed1b-d6fd-45de-8342-5f10cfed3c07")
    )
    assert record_uses_gateway_models(_record(command="cc-go-ulw"))
    assert not record_uses_gateway_models(_record(command="printf cc-go-ulw"))
    assert not record_uses_gateway_models(_record(command="codex --yolo"))
    assert not record_uses_gateway_models(
        SimpleNamespace(command="cc-go-ulw", recovery_command=None, external=True)
    )


def test_status_marks_only_pre_cache_process_as_stale(tmp_path: Path) -> None:
    cache = tmp_path / "gateway-models.json"
    _write_cache(cache, [f"claude-model-{index}" for index in range(43)])
    os.utime(cache, (2_000.0, 2_000.0))
    record = _record(recovery="cc-go-ulw --resume d2a7ed1b-d6fd-45de-8342-5f10cfed3c07")

    stale = gateway_model_status(record, process_started_at=1_000.0, cache_path=cache)
    fresh = gateway_model_status(record, process_started_at=3_000.0, cache_path=cache)

    assert stale.applicable and stale.available and stale.stale
    assert stale.model_count == 43
    assert not fresh.stale
    assert fresh.fingerprint == stale.fingerprint


def test_proc_start_epoch_is_close_to_this_process_start() -> None:
    started = process_start_epoch(os.getpid())
    assert started is not None
    assert 0 <= (os.path.getmtime(f"/proc/{os.getpid()}") - started) < 60


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_inventory_rejects_invalid_fetched_at_values(
    tmp_path: Path, value: object
) -> None:
    cache = tmp_path / "models.json"
    cache.write_text(
        json.dumps(
            {
                "baseUrl": "http://127.0.0.1:8318",
                "fetchedAt": value,
                "models": [{"id": "claude-a"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GatewayModelError, match="fetchedAt"):
        load_gateway_model_inventory(cache)


def test_inventory_rejects_oversized_base_url(tmp_path: Path) -> None:
    cache = tmp_path / "models.json"
    cache.write_text(
        json.dumps(
            {
                "baseUrl": "x" * 4097,
                "models": [{"id": "claude-a"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GatewayModelError, match="baseUrl"):
        load_gateway_model_inventory(cache)
