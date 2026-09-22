#!/usr/bin/env python3
"""Render TermDeck in an isolated Xvfb session and capture a screenshot.

This is a developer visual-smoke harness, not an application runtime dependency.
It isolates HOME, the session database, and the tmux socket, then cleans up all
processes even when rendering or screenshot capture fails.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


_SMOKE_MARKERS = (
    "TERMDECK_UI_SMOKE_SHELL",
    "TERMDECK_UI_SMOKE_TESTS",
    "TERMDECK_UI_SMOKE_SERVER",
)


def _require(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"required executable not found: {name}")
    return path


def _wait_for_image(path: Path, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 1024:
            return
        time.sleep(0.1)
    raise RuntimeError(f"screenshot was not created: {path}")


def _parse_identify_size(value: str) -> tuple[int, int]:
    match = re.search(r"\b(\d+)x(\d+)\b", value)
    if not match:
        raise RuntimeError(f"could not parse screenshot dimensions from identify output: {value!r}")
    return int(match.group(1)), int(match.group(2))


def _classify_window_size(
    requested: tuple[int, int] | None,
    actual: tuple[int, int],
) -> tuple[list[str], list[str]]:
    """Separate a GTK minimum-size adjustment from a clipped capture.

    ``xdotool windowsize`` is a request.  GTK is allowed to keep a larger
    window when the header/terminal layout has a real minimum width, so an
    expansion is useful evidence rather than a harness failure.  A capture
    smaller than requested is different: it means the screenshot was clipped
    and should fail the smoke.
    """

    if requested is None or actual == requested:
        return [], []
    requested_width, requested_height = requested
    actual_width, actual_height = actual
    if actual_width < requested_width or actual_height < requested_height:
        return [f"requested {requested_width}x{requested_height}, captured {actual_width}x{actual_height}"], []
    return [], [
        "GTK kept its minimum window geometry: "
        f"requested {requested_width}x{requested_height}, captured {actual_width}x{actual_height}"
    ]


def _tmux_pane_state(tmux: str, env: dict[str, str]) -> tuple[list[str], set[str], str]:
    """Return pane ids and visible smoke markers from the isolated tmux server."""

    panes_result = subprocess.run(
        [tmux, "list-panes", "-a", "-F", "#{pane_id}"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
    )
    if panes_result.returncode != 0:
        detail = panes_result.stderr.strip() or panes_result.stdout.strip() or "no tmux panes"
        raise RuntimeError(f"isolated tmux pane query failed: {detail}")
    pane_ids = [line.strip() for line in panes_result.stdout.splitlines() if line.strip()]
    captured: list[str] = []
    for pane_id in pane_ids:
        capture_result = subprocess.run(
            [tmux, "capture-pane", "-p", "-t", pane_id, "-S", "-30"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
        if capture_result.returncode == 0:
            captured.append(capture_result.stdout)
    visible = "\n".join(captured)
    markers = {marker for marker in _SMOKE_MARKERS if marker in visible}
    return pane_ids, markers, visible


def _wait_for_smoke_panes(
    tmux: str,
    env: dict[str, str],
    *,
    timeout: float = 12.0,
) -> dict[str, object]:
    """Wait until all workspace panes have rendered visible, known text.

    A mapped GTK window with a blank terminal is not a useful visual result.
    This bounded readiness check catches failed tmux startup and prevents the
    harness from declaring an error toast or empty pane a passing screenshot.
    """

    deadline = time.monotonic() + timeout
    last_pane_ids: list[str] = []
    last_markers: set[str] = set()
    while time.monotonic() < deadline:
        pane_ids, markers, _visible = _tmux_pane_state(tmux, env)
        last_pane_ids = pane_ids
        last_markers = markers
        if len(pane_ids) >= len(_SMOKE_MARKERS) and markers == set(_SMOKE_MARKERS):
            return {
                "pane_count": len(pane_ids),
                "markers": sorted(markers),
            }
        time.sleep(0.2)
    expected = ", ".join(_SMOKE_MARKERS)
    observed = ", ".join(sorted(last_markers)) or "none"
    raise RuntimeError(
        "visual smoke terminal panes did not become ready: "
        f"panes={len(last_pane_ids)}, markers={observed}, expected={expected}"
    )


def _terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=4)


def _start_xvfb(
    executable: str,
    *,
    requested_display: str | None,
    width: int,
    height: int,
    env: dict[str, str],
) -> tuple[subprocess.Popen[str], str]:
    candidates = [requested_display] if requested_display else [f":{number}" for number in range(97, 130)]
    failures: list[str] = []
    for display in candidates:
        if not display:
            continue
        process = subprocess.Popen(
            [executable, display, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
            env={**env, "DISPLAY": display},
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        time.sleep(0.45)
        if process.poll() is None:
            return process, display
        detail = process.stderr.read().strip() if process.stderr else "unknown Xvfb error"
        failures.append(f"{display}: {detail}")
    joined = " | ".join(failures[-4:])
    raise RuntimeError(f"Xvfb failed on all candidate displays: {joined}")


def _wait_for_window(xwininfo: str, env: dict[str, str], timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    last_tree = ""
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [xwininfo, "-display", env["DISPLAY"], "-root", "-tree"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            time.sleep(0.15)
            continue
        last_tree = result.stdout
        fallback_windows: list[tuple[int, str]] = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped.startswith("0x"):
                continue
            window_id = stripped.split()[0]
            if "t1000" in stripped.casefold() or "termdeck" in stripped.casefold():
                return window_id
            geometry = re.search(r"\s(\d+)x(\d+)[+-]-?\d+[+-]-?\d+", stripped)
            if geometry:
                area = int(geometry.group(1)) * int(geometry.group(2))
                if area >= 400 * 300:
                    fallback_windows.append((area, window_id))
        if fallback_windows:
            return max(fallback_windows)[1]
        time.sleep(0.15)
    tree_tail = "\n".join(last_tree.splitlines()[-80:]).strip()
    raise RuntimeError(
        "TermDeck window did not become visible in the virtual display; "
        f"X tree tail: {tree_tail or 'empty'}"
    )


def render(
    output: Path,
    *,
    display: str | None = None,
    width: int = 1440,
    height: int = 900,
    window_width: int | None = None,
    window_height: int | None = None,
) -> dict[str, object]:
    if (window_width is None) != (window_height is None):
        raise ValueError("window_width and window_height must be provided together")
    xvfb = _require("Xvfb")
    imagemagick_import = _require("import")
    identify = _require("identify")
    tmux = _require("tmux")
    dbus_run_session = _require("dbus-run-session")
    xwininfo = _require("xwininfo")
    xdotool = _require("xdotool") if window_width is not None else None

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="termdeck-ui-smoke-") as temp_dir:
        temp = Path(temp_dir)
        home = temp / "home"
        tmux_tmp = temp / "tmux"
        runtime_dir = temp / "runtime"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        runtime_dir.mkdir(mode=0o700)
        db_path = temp / "sessions.sqlite3"
        workspace_path = temp / "workspace.json"
        workspace_path.write_text(
            json.dumps(
                {
                    "name": "Visual QA",
                    "windows": [
                        {
                            "title": "Design Review",
                            "tabs": [
                                {
                                    "name": "ui tests",
                                    "tag": "quality",
                                    "command": f"{sys.executable} -c 'import time; print(\"TERMDECK_UI_SMOKE_TESTS\", flush=True); time.sleep(60)'",
                                    "cwd": str(Path.cwd()),
                                },
                                {
                                    "name": "dev server",
                                    "tag": "frontend",
                                    "command": "printf 'TERMDECK_UI_SMOKE_SERVER\\n'; echo 'Ready on http://127.0.0.1:4173'; sleep 60",
                                    "cwd": str(Path.cwd()),
                                },
                                {
                                    "name": "shell",
                                    "tag": "workspace",
                                    # Leave a real shell command selected for
                                    # the screenshot so the compact smoke
                                    # proves terminal text is painted, not
                                    # merely present in a detached tmux pane.
                                    "command": "printf 'TERMDECK_UI_SMOKE_SHELL\\n'; sleep 60",
                                    "cwd": str(Path.cwd()),
                                    "pinned": True,
                                },
                            ],
                        }
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "TMUX_TMPDIR": str(tmux_tmp),
                # Keep this smoke independent from the host user manager.  The
                # application deliberately falls back to direct tmux when the
                # user-systemd private socket is absent; inheriting the host
                # XDG_RUNTIME_DIR made systemd-run race the disposable smoke.
                "XDG_RUNTIME_DIR": str(runtime_dir),
                "GTK_CSD": "1",
                "GDK_BACKEND": "x11",
                "NO_AT_BRIDGE": "1",
            }
        )

        xvfb_process: subprocess.Popen[str] | None = None
        app_process: subprocess.Popen[str] | None = None
        app_stdout = temp / "app.stdout"
        app_stderr = temp / "app.stderr"
        active_display = display
        try:
            xvfb_process, active_display = _start_xvfb(
                xvfb,
                requested_display=display,
                width=width,
                height=height,
                env=env,
            )
            env["DISPLAY"] = active_display

            with app_stdout.open("w", encoding="utf-8") as stdout_handle, app_stderr.open(
                "w", encoding="utf-8"
            ) as stderr_handle:
                app_process = subprocess.Popen(
                    [
                        dbus_run_session,
                        "--",
                        sys.executable,
                        "-m",
                        "termdeck.cli",
                        "gui",
                        "--db",
                        str(db_path),
                        "--workspace",
                        str(workspace_path),
                    ],
                    env=env,
                    cwd=Path.cwd(),
                    text=True,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                )
                time.sleep(1.0)
                if app_process.poll() is not None:
                    raise RuntimeError(
                        "TermDeck exited before capture: " + app_stderr.read_text(encoding="utf-8", errors="replace")
                    )
                try:
                    window_id = _wait_for_window(xwininfo, env)
                except RuntimeError as exc:
                    detail = app_stderr.read_text(encoding="utf-8", errors="replace").strip()
                    raise RuntimeError(f"{exc}; application stderr: {detail or 'empty'}") from exc
                if xdotool is not None and window_width is not None and window_height is not None:
                    subprocess.run(
                        [xdotool, "windowsize", "--sync", window_id, str(window_width), str(window_height)],
                        env=env,
                        check=True,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=10,
                    )
                time.sleep(0.8)
                smoke_state = _wait_for_smoke_panes(tmux, env)
                # Give the VTE paint pass one frame after tmux readiness.  At
                # the compact minimum, the final GTK resize can finish just
                # after the pane output is observable through tmux.
                time.sleep(0.35)

                subprocess.run(
                    [imagemagick_import, "-display", active_display, "-window", window_id, str(output)],
                    env=env,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                )
                _wait_for_image(output)

            identify_result = subprocess.run(
                [identify, "-format", "%m %wx%h %[mean]", str(output)],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            identify_text = identify_result.stdout.strip()
            actual_width, actual_height = _parse_identify_size(identify_text)
            stderr_text = app_stderr.read_text(encoding="utf-8", errors="replace")
            diagnostic_lines = [
                line
                for line in stderr_text.splitlines()
                if any(token in line.casefold() for token in ("warning", "critical", "error", "traceback"))
            ]
            external_markers = ("xdg-desktop-portal", "dbus-daemon", "pipewire")
            application_error_lines = [
                line
                for line in diagnostic_lines
                if not any(marker in line.casefold() for marker in external_markers)
            ]
            requested_size = (
                (window_width, window_height)
                if window_width is not None and window_height is not None
                else None
            )
            layout_error_lines, layout_warning_lines = _classify_window_size(
                requested_size,
                (actual_width, actual_height),
            )
            return {
                "ok": not application_error_lines and not layout_error_lines,
                "display": active_display,
                "output": str(output),
                "bytes": output.stat().st_size,
                "identify": identify_text,
                "requested_window_size": (
                    f"{window_width}x{window_height}" if window_width is not None and window_height is not None else None
                ),
                "actual_window_size": f"{actual_width}x{actual_height}",
                "application_error_lines": application_error_lines,
                "layout_error_lines": layout_error_lines,
                "layout_warning_lines": layout_warning_lines,
                "smoke_pane_count": smoke_state["pane_count"],
                "smoke_markers": smoke_state["markers"],
                "environment_warning_count": len(diagnostic_lines) - len(application_error_lines),
            }
        finally:
            _terminate(app_process)
            subprocess.run(
                [tmux, "kill-server"],
                env=env,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            _terminate(xvfb_process)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/termdeck-ui-smoke.png"))
    parser.add_argument("--display", default=None, help="explicit X display; default chooses a free display")
    parser.add_argument("--window-width", type=int, default=None, help="resize the mapped window before capture")
    parser.add_argument("--window-height", type=int, default=None, help="resize the mapped window before capture")
    args = parser.parse_args()
    try:
        result = render(
            args.output,
            display=args.display,
            window_width=args.window_width,
            window_height=args.window_height,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
