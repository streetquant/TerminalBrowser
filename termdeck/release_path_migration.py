"""Authenticated path migration for relocated, pre-wrapper managed releases.

Historical paths are rendering inputs, never filesystem destinations. The copied
wheel authenticates the old activation; the normal release-manager transaction
publishes the replacement. This is deliberately not a generic receipt rewriter.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import release_manager as manager
from .build_info import canonical_json_bytes, sha256_bytes


@dataclass(frozen=True)
class LegacyPathMigration:
    source_layout: manager.ReleaseLayout
    activation: manager._ActivationBundle
    preimages: Mapping[Path, manager._FileSnapshot]
    parent_guards: object
    receipt_path: Path
    absent_wrapper_paths: tuple[Path, ...]
    absent_history_paths: tuple[Path, ...]
    state_path: Path

    def revalidate(self) -> None:
        """Reject replacement of an authenticated preimage before publication."""
        observed = manager._capture_files_safely(
            tuple((path, "path migration preimage") for path in self.preimages),
            parent_guards=self.parent_guards,
        )
        if observed != self.preimages:
            raise manager.ReleaseError("legacy path migration preimage changed")
        for path in (*self.absent_wrapper_paths, *self.absent_history_paths):
            if manager._path_lexists(path):
                raise manager.ReleaseError(
                    f"legacy path migration destination appeared during preflight: {path}"
                )

    def receipt(self, layout: manager.ReleaseLayout, state: Mapping[str, object]) -> bytes:
        """Keep the old bytes as audit evidence, not as future execution authority."""
        return canonical_json_bytes({
            "schema": "t1000.legacy-path-migration.v1",
            "source_root": str(self.source_layout.root),
            "source_bin_dir": str(self.source_layout.bin_dir),
            "target_root": str(layout.root),
            "target_bin_dir": str(layout.bin_dir),
            "prior_state_sha256": sha256_bytes(self.preimages[self.state_path].content),
            "published_state_sha256": sha256_bytes(canonical_json_bytes(state)),
            "current_release_id": state["current_release_id"],
            "preimages": [
                {
                    "path": str(path),
                    "exists": snapshot.exists,
                    "mode": snapshot.mode,
                    "sha256": sha256_bytes(snapshot.content),
                    "content_base64": base64.b64encode(snapshot.content).decode("ascii"),
                }
                for path, snapshot in sorted(self.preimages.items(), key=lambda item: str(item[0]))
                if path != self.receipt_path
            ],
            "historical_paths_written": False,
            "live_processes_restarted": False,
        })


def _historical_path(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise manager.ReleaseError(f"{label} must be an explicit absolute path without traversal")
    return path


def prepare_legacy_path_migration(
    layout: manager.ReleaseLayout,
    state: Mapping[str, object],
    interpreter: Path,
    *,
    source_root: str | Path,
    source_bin_dir: str | Path,
) -> LegacyPathMigration:
    """Authenticate a copied legacy activation without reading its retired paths.

    Already-managed wrapper histories and conventional legacy-launcher backups
    have their own path-bound contracts. They are rejected, never rewritten or
    discarded. The supported input is the pre-wrapper layout used by the
    migrated t1000 installation: state, bootstrap, immutable releases and an
    optional copied stable launcher.
    """
    source = manager.release_layout(
        root=_historical_path(source_root, "migration source root"),
        bin_dir=_historical_path(source_bin_dir, "migration source bin directory"),
    )
    if source.root == layout.root and source.bin_dir == layout.bin_dir:
        raise manager.ReleaseError("legacy path migration requires a changed root or launcher directory")
    current = state.get("current_release_id")
    if not isinstance(current, str) or manager._RELEASE_ID.fullmatch(current) is None:
        raise manager.ReleaseError("legacy path migration requires an installed release")
    if state.get("launcher_path") != str(source.launcher_path):
        raise manager.ReleaseError("legacy path migration source launcher does not match managed state")
    if state.get("python_executable") != str(interpreter):
        raise manager.ReleaseError("legacy path migration cannot change the Python interpreter")
    if state.get("legacy_launcher") is not None:
        raise manager.ReleaseError("legacy path migration cannot rewrite a legacy-launcher backup")

    history_paths = (
        manager._legacy_wrapper_preimage_path(layout),
        manager._first_install_preimage_path(layout),
    )
    for path in history_paths:
        if manager._path_lexists(path):
            raise manager.ReleaseError("legacy path migration cannot rewrite existing path-bound history")
    api = manager._service_wrapper_api()
    # The positively identified pre-wrapper release never shipped the module
    # required by modern wrapper execution.  An absent wrapper set is the
    # only supported baseline; this does not render or execute an A wrapper.
    profile = manager._verified_pre_wrapper_profile(layout, current)
    kwargs = manager._wrapper_layout_kwargs(layout, None if profile else current)
    wrapper_paths = tuple(manager._wrapper_path_set(api, kwargs, current))
    if any(manager._path_lexists(path) for path in wrapper_paths):
        raise manager.ReleaseError("legacy path migration requires an absent managed wrapper set")

    # The state digest alone is not authority. Validate its selected immutable
    # artifact and derive the old executable bytes from that exact wheel.
    release = layout.releases_dir / current
    manager.verify_installed_tree(release)
    manifest_hash = manager._regular_file_hash(release / "manifest.json", label="migration manifest")
    if manifest_hash != state.get("manifest_sha256"):
        raise manager.ReleaseError("legacy path migration manifest does not match managed state")
    activation = manager._derive_release_activation_bundle(
        layout, current, interpreter, render_layout=source
    )
    if (
        activation.bootstrap_sha256 != state.get("bootstrap_sha256")
        or activation.launcher_sha256 != state.get("launcher_sha256")
    ):
        raise manager.ReleaseError("legacy path migration state is not the canonical historical activation")
    manager._regular_file_hash(layout.bootstrap_path, label="migration bootstrap", executable=True)
    manager._optional_regular_hash(layout.launcher_path, label="migration launcher", executable=True)

    state_bytes = canonical_json_bytes(state)
    receipt_path = layout.root / f"path-migration-{sha256_bytes(state_bytes)}.json"
    paths = (*manager._activation_publication_paths(layout), receipt_path)
    guards = manager._capture_publication_parent_guards(paths, create_parents=True)
    snapshots = manager._capture_files_safely(
        tuple((path, "path migration preimage") for path in paths),
        parent_guards=guards,
    )
    if snapshots[layout.state_path].content != state_bytes:
        raise manager.ReleaseError("legacy path migration state changed during preflight")
    if snapshots[layout.bootstrap_path].content != activation.bootstrap:
        raise manager.ReleaseError("legacy path migration bootstrap is not canonical")
    launcher = snapshots[layout.launcher_path]
    # A cutover may already have rendered the canonical new launcher while the
    # copied bootstrap/state still bind the old root. Neither variant permits
    # arbitrary existing executable bytes to be overwritten.
    allowed_launchers = {activation.launcher, manager._launcher_source(layout, interpreter)}
    if launcher.exists and launcher.content not in allowed_launchers:
        raise manager.ReleaseError("legacy path migration copied launcher is not canonical")
    if snapshots[receipt_path].exists:
        raise manager.ReleaseError("legacy path migration receipt already exists; reconcile before retrying")
    plan = LegacyPathMigration(
        source, activation, snapshots, guards, receipt_path,
        wrapper_paths, history_paths, layout.state_path,
    )
    plan.revalidate()
    return plan
