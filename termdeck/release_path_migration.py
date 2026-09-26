"""Authenticated path migration for relocated, pre-wrapper managed releases.

Historical paths are rendering inputs, never filesystem destinations. The copied
wheel authenticates the old activation; the normal release-manager transaction
publishes the replacement. This is deliberately not a generic receipt rewriter.
"""
from __future__ import annotations

import base64
import os
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
    anchored_preimages: Mapping[Path, manager._AnchoredFileSnapshot]
    autostart_paths: tuple[Path, ...] = ()

    def revalidate(self) -> None:
        """Reject replacement of an authenticated preimage before publication."""
        observed = manager._capture_anchored_files_safely(
            tuple((path, "path migration preimage") for path in self.preimages),
            parent_guards=self.parent_guards,
        )
        if observed != self.anchored_preimages:
            raise manager.ReleaseError("legacy path migration preimage changed")
        for path in (*self.absent_wrapper_paths, *self.absent_history_paths):
            if manager._path_lexists(path):
                raise manager.ReleaseError(
                    f"legacy path migration destination appeared during preflight: {path}"
                )

    def authorizes_legacy_autostart(
        self, layout, prior_state, descriptor_path, descriptor_preimage, receipt_path, receipt_preimage,
    ) -> bool:
        if self.autostart_paths != (descriptor_path, receipt_path):
            return False
        self.revalidate()
        return (
            layout.state_path == self.state_path
            and canonical_json_bytes(prior_state) == self.preimages[self.state_path].content
            and descriptor_preimage == self.preimages[descriptor_path]
            and receipt_preimage == self.preimages[receipt_path]
            and not receipt_preimage.exists
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


def _captured_pre_wrapper_variant(
    activation: manager._ActivationBundle,
    source: manager.ReleaseLayout,
    profile: Mapping[str, object] | None,
) -> manager._ActivationBundle | None:
    """Authenticate the single captured pre-state-environment rendering.

    The exact c9d620 wheel renders a later state-root assignment/export that
    the copied deployment predates. Derive that earlier pair from verified
    wheel output, then require its independently captured hashes. Neither a
    self-consistent state digest nor arbitrary environment-line removal grants
    admission. This variant is only a migration preimage, never new code.
    """
    if (
        profile is None
        or profile.get("release_id") != "c9d6204ee6dbe01dc4225970cb41014e759d38146bef824b5fbda71cb3ed707c"
        or profile.get("wheel_sha256") != "491fdd87ef8b97d488a456f1d5f893f284bf227693d2e7f7a61a30b2b09c080e"
        or source.root != Path("/warp/runtime/termdeck/t1000-managed")
        or source.bin_dir != Path("/warp/runtime/termdeck/bin")
        or activation.bootstrap_sha256 != "2a2dc0cad3dc7764a14ce6f1d04e3796705cd44862ef3a524635452bf5b5bf4f"
        or activation.launcher_sha256 != "05e7465cd927566b535ddbd3fb563cd54258a3eb196ec7a6aee80fa336807696"
    ):
        return None
    bootstrap_line = b'os.environ["TERMDECK_STATE_ROOT"] = \'/warp/state/termdeck\'\n'
    launcher_line = b"export TERMDECK_STATE_ROOT=/warp/state/termdeck\n"
    if activation.bootstrap.count(bootstrap_line) != 1 or activation.launcher.count(launcher_line) != 1:
        return None
    variant = manager._activation_bundle(
        activation.bootstrap.replace(bootstrap_line, b"", 1),
        activation.launcher.replace(launcher_line, b"", 1),
        origin="selected-release",
    )
    if (
        variant.bootstrap_sha256 != "a988252c12712352145bc40b4eacf5b1d6f68a391bcc8823b92d7c12b7fc13f2"
        or variant.launcher_sha256 != "02ae446e4580bfe8f4ef1d5a6b56bb679bc0b6517242a007519ca4d9d60aa5e0"
    ):
        return None
    return variant


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
        variant = _captured_pre_wrapper_variant(activation, source, profile)
        if (
            variant is None
            or variant.bootstrap_sha256 != state.get("bootstrap_sha256")
            or variant.launcher_sha256 != state.get("launcher_sha256")
        ):
            raise manager.ReleaseError("legacy path migration state is not the canonical historical activation")
        activation = variant
    manager._regular_file_hash(layout.bootstrap_path, label="migration bootstrap", executable=True)
    manager._optional_regular_hash(layout.launcher_path, label="migration launcher", executable=True)

    state_bytes = canonical_json_bytes(state)
    receipt_path = layout.root / f"path-migration-{sha256_bytes(state_bytes)}.json"
    autostart_paths = ()
    if (
        profile is not None
        and layout.root == manager._DEPLOYMENT_MANAGED_ROOT
        and sha256_bytes(state_bytes) == "8809017c57fe0ac430909ff006fc652fadebd1e37e5ac1487f5926ebc9093e43"
    ):
        # This copied generation predates the autostart receipt contract.
        # Admit its one captured desktop only as part of this authenticated
        # migration, never as arbitrary existing-generation receipt recovery.
        desktop = manager._production_autostart_path()
        if desktop != Path("/home/arch/.config/autostart/io.github.t1000.T1000.desktop"):
            raise manager.ReleaseError("legacy autostart descriptor path is not the captured path")
        autostart_paths = (desktop, manager._production_autostart_receipt_path(layout))
    paths = (*manager._activation_publication_paths(layout), receipt_path, *autostart_paths)
    guards = manager._capture_publication_parent_guards(paths, create_parents=True)
    anchored = manager._capture_anchored_files_safely(
        tuple((path, "path migration preimage") for path in paths),
        parent_guards=guards,
    )
    snapshots = {path: capture.snapshot for path, capture in anchored.items()}
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
    if autostart_paths:
        desktop, autostart_receipt = autostart_paths
        image = snapshots[desktop]
        metadata = desktop.lstat()
        if (
            not image.exists or image.mode != 0o644
            or sha256_bytes(image.content) != "1b59f7367ffcb07cee4e7b1eed984400dfdd37e5b536305a0e9ed44a70d2f96d"
            or (metadata.st_uid, metadata.st_gid, metadata.st_nlink) != (1000, 985, 1)
            or metadata.st_uid != os.geteuid()
            or snapshots[autostart_receipt].exists
        ):
            raise manager.ReleaseError("legacy autostart preimage is not the captured descriptor and absent receipt")
    plan = LegacyPathMigration(
        source, activation, snapshots, guards, receipt_path,
        wrapper_paths, history_paths, layout.state_path, anchored, autostart_paths,
    )
    plan.revalidate()
    return plan
