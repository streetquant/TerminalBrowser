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
    def failed_count(self) -> int:
        return sum(1 for row in self.checkpoints if not row.ok)

    def as_dict(self) -> dict[str, object]:
        return {
            "metadata_updates": self.metadata_updates,
            "checkpoint_count": len(self.checkpoints),
            "exact_count": self.exact_count,
            "failed_count": self.failed_count,
            "sessions": [row.as_dict() for row in self.checkpoints],
        }


def persistence_candidates(store: SessionStore) -> list[SessionRecord]:
    """Return managed records whose process state may still be live."""

    return [
        record
        for record in store.list(include_archived=False, include_stopped=False)
        if not getattr(record, "external", False)
    ]


def persist_live_sessions(
    store: SessionStore,
    backend,
    *,
    records: Sequence[SessionRecord] | None = None,
) -> PersistenceResult:
    """Persist exact Codex IDs, then checkpoint every other live process tree.

    The ordering is essential. A final unbounded observation writes each primary
    Codex UUID before checkpoint selection; the subsequent refreshed records let
    the backend use deterministic ``codex resume <UUID>`` instead of a memory
    checkpoint or a new/random Codex session.
    """

    selected = list(records) if records is not None else persistence_candidates(store)
    metadata_updates = 0
    observe = getattr(backend, "recovery_metadata", None)
    if selected and callable(observe):
        try:
            observed = dict(observe(selected, bounded=False))
        except TypeError:
            # Compatibility with small third-party/fake backends that predate
            # the explicit unbounded shutdown scan.
            observed = dict(observe(selected))
        except Exception:
            observed = {}
        updates = [
            (session_id, command, cwd if command else None)
            for session_id, (command, cwd) in observed.items()
        ]
        if updates:
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
