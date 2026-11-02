from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .checkpointing import CheckpointResult
from .models import SessionRecord
from .session_store import SessionStore


@dataclass(frozen=True)
class PersistenceResult:
    metadata_updates: int
    checkpoints: tuple[CheckpointResult, ...]

    @property
    def exact_count(self) -> int:
        return sum(1 for row in self.checkpoints if row.ok and row.exact)

    @property
    def unsupported_count(self) -> int:
        return sum(1 for row in self.checkpoints if row.action == "unsupported")

    @property
    def failed_count(self) -> int:
        return sum(
            1
            for row in self.checkpoints
            if not row.ok and row.action != "unsupported"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "metadata_updates": self.metadata_updates,
            "checkpoint_count": len(self.checkpoints),
            "exact_count": self.exact_count,
            "unsupported_count": self.unsupported_count,
            "failed_count": self.failed_count,
            "sessions": [row.as_dict() for row in self.checkpoints],
        }


def persistence_candidates(store: SessionStore) -> list[SessionRecord]:
    """Return managed records whose process state may still be live."""

    live_statuses = {"running", "paused", "missing"}
    return [
        record
        for record in store.list(include_archived=False, include_stopped=False)
        if not getattr(record, "external", False)
        and record.status.casefold() in live_statuses
    ]


def persist_live_sessions(
    store: SessionStore,
    backend,
    *,
    records: Sequence[SessionRecord] | None = None,
) -> PersistenceResult:
    """Persist generic recipes, optional native resume metadata, and checkpoints.

    Every managed terminal first receives a canonical command/cwd/environment
    recipe that can recreate it without knowing the owning application. A final
    bounded process observation may then enrich that baseline with an opaque
    application-native resume command, and an exact process checkpoint remains
    an optional higher-fidelity layer. None of these steps requires Codex,
    Claude, CodexPro, or any other agent executable.
    """

    selected = list(records) if records is not None else persistence_candidates(store)
    prepared: list[SessionRecord] = []
    for record in selected:
        try:
            prepared.append(store.ensure_recovery_recipe(record.id))
        except Exception:
            current = store.get(record.id)
            if current is not None:
                prepared.append(current)
    selected = prepared
    metadata_updates = 0
    observe = getattr(backend, "recovery_metadata", None)
    observe_batch = getattr(backend, "recovery_observations", None)
    authority = {}
    if callable(observe_batch):
        observe = observe_batch
    if selected and callable(observe):
        try:
            if callable(observe_batch):
                result = observe(selected, bounded=False, native_capture=False)
            else:
                result = observe(selected, bounded=False)
            if callable(observe_batch):
                authority = dict(result.authority)
                observed = dict(result.metadata)
            else:
                observed = dict(result)
        except TypeError:
            # Compatibility with small third-party/fake backends that predate
            # the explicit unbounded shutdown scan.
            result = observe(selected)
            if callable(observe_batch):
                authority = dict(result.authority)
                observed = dict(result.metadata)
            else:
                observed = dict(result)
        except Exception:
            observed = {}
        # Shutdown observation races with desktop/session teardown. A readable
        # fallback shell after an application has received SIGTERM is not proof
        # that a previously persisted native resume target is obsolete. Final
        # persistence may add or strengthen opaque resume metadata, but it never
        # erases the canonical generic recipe.
        updates = [
            (session_id, command, cwd)
            for session_id, (command, cwd) in observed.items()
            if command is not None
        ]
        if updates:
            if authority:
                metadata_updates = store.update_recovery_metadata(updates, authority_observations=authority)
            else:
                metadata_updates = store.update_recovery_metadata(updates)

    refreshed: list[SessionRecord] = []
    for record in selected:
        current = store.get(record.id)
        if current is not None:
            refreshed.append(current)

    checkpoint = getattr(backend, "checkpoint_records", None)
    if not refreshed or not callable(checkpoint):
        return PersistenceResult(metadata_updates, ())
    try:
        results = tuple(checkpoint(refreshed))
    except Exception as exc:
        results = tuple(
            CheckpointResult(
                record.id,
                "checkpoint",
                False,
                False,
                f"checkpoint operation failed: {exc}",
            )
            for record in refreshed
        )
    return PersistenceResult(metadata_updates, results)
