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
            if "termdeck" in stripped.casefold():
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
) -> dict[str, object]:
    xvfb = _require("Xvfb")
    imagemagick_import = _require("import")
    identify = _require("identify")
    tmux = _require("tmux")
    dbus_run_session = _require("dbus-run-session")
    xwininfo = _require("xwininfo")

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="termdeck-ui-smoke-") as temp_dir:
        temp = Path(temp_dir)
        home = temp / "home"
        tmux_tmp = temp / "tmux"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
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
                                    "name": "shell",
                                    "tag": "workspace",
                                    "command": "{shell}",
                                    "cwd": str(Path.cwd()),
                                    "pinned": True,
                                },
                                {
                                    "name": "ui tests",
                                    "tag": "quality",
                                    "command": f"{sys.executable} -c 'import time; print(\"UI smoke session\"); time.sleep(60)'",
                                    "cwd": str(Path.cwd()),
                                },
                                {
                                    "name": "dev server",
                                    "tag": "frontend",
                                    "command": "sh -c 'echo Ready on http://127.0.0.1:4173; sleep 60'",
                                    "cwd": str(Path.cwd()),
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
                time.sleep(0.8)

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
            return {
                "ok": not application_error_lines,
                "display": active_display,
                "output": str(output),
                "bytes": output.stat().st_size,
                "identify": identify_result.stdout.strip(),
                "application_error_lines": application_error_lines,
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
    args = parser.parse_args()
    try:
        result = render(args.output, display=args.display)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
