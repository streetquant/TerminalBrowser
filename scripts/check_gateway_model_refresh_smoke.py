#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import time

from termdeck.models import SessionRecord
from termdeck.resources import observe_recovery_metadata
from termdeck.session_recipe import (
    build_session_recipe,
    encode_session_recipe,
)
from termdeck.tmux_backend import TmuxBackend


SESSION_ID = "d2a7ed1b-d6fd-45de-8342-5f10cfed3c07"


def wait_until(predicate, *, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.025)
    raise RuntimeError(f"condition was not satisfied; last={last!r}")


def main() -> int:
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not compiler:
        raise RuntimeError("a C compiler is required for the disposable refresh smoke")
    backend = TmuxBackend(executable="tmux", command_timeout=5.0, query_timeout=1.0)
    session_name = f"termdeck_gateway_refresh_smoke_{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="termdeck-gateway-refresh-") as raw_tmp:
        root = Path(raw_tmp).resolve()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        source = root / "cc-go-ulw.c"
        executable = bin_dir / "cc-go-ulw"
        source.write_text(
            r'''#include <stdio.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("READY:%ld\n", (long)getpid());
    char line[1024];
    while (fgets(line, sizeof(line), stdin) != NULL) {
        printf("ECHO:%s", line);
    }
    return 0;
}
''',
            encoding="utf-8",
        )
        subprocess.run(
            [compiler, "-O2", "-Wall", "-Wextra", "-o", str(executable), str(source)],
            check=True,
            capture_output=True,
            text=True,
        )
        exact = f"cc-go-ulw --resume {SESSION_ID}"
        launch_path = f"{bin_dir}:/usr/bin:/bin"
        # Bind command resolution inside the shell command itself. Host shell
        # startup files are then unable to select the user's real cc-go-ulw.
        launch_command = (
            f"PATH={shlex.quote(launch_path)} exec cc-go-ulw --resume {SESSION_ID}"
        )
        recipe_object = build_session_recipe(
            launch_command,
            root,
            environment={"PATH": launch_path},
            restart_policy="on-failure",
            origin="native-resume",
        )
        recipe, digest = encode_session_recipe(recipe_object)
        policy = recipe_object.restart_policy
        record = SessionRecord(
            id="td-gateway-refresh-smoke",
            name="gateway refresh smoke",
            tag="test",
            command="cc-go-ulw",
            cwd=str(root),
            tmux_name=session_name,
            created_at="now",
            last_opened_at="now",
            status="recovery-pending",
            recovery_command=exact,
            recovery_cwd=str(root),
            recovery_recipe=recipe,
            recovery_recipe_sha256=digest,
            restart_policy=policy,
        )
        try:
            backend._run(
                [
                    "new-session",
                    "-d",
                    "-s",
                    session_name,
                    "-e",
                    f"PATH={launch_path}",
                    "-c",
                    str(root),
                    launch_command,
                ]
            )
            backend._run(["set-option", "-t", session_name, "remain-on-exit", "on"])
            before = wait_until(lambda: backend._inspect_auth_pane(record))
            observed_before = wait_until(
                lambda: (
                    metadata
                    if (metadata := observe_recovery_metadata(before.pane_pid))[1] == exact
                    else None
                )
            )
            before_capture = wait_until(
                lambda: (
                    text
                    if "READY:" in (text := backend.capture(record, lines=30))
                    else None
                )
            )

            replacement_pid = backend.restart_exact_payload(record)
            after = wait_until(lambda: backend._inspect_auth_pane(record))
            observed_after = wait_until(
                lambda: (
                    metadata
                    if (metadata := observe_recovery_metadata(after.pane_pid))[1] == exact
                    else None
                )
            )
            backend._run(
                ["send-keys", "-t", backend.pane_target(record), "-l", "model-refresh-probe"]
            )
            backend._run(["send-keys", "-t", backend.pane_target(record), "C-m"])
            after_capture = wait_until(
                lambda: (
                    text
                    if "ECHO:model-refresh-probe" in (
                        text := backend.capture(record, lines=40)
                    )
                    else None
                )
            )

            if after.pane_id != before.pane_id:
                raise RuntimeError("refresh changed the tmux pane id")
            if replacement_pid != after.pane_pid or after.pane_pid == before.pane_pid:
                raise RuntimeError("refresh did not prove a new pane process")
            if observed_before[2] != str(root) or observed_after[2] != str(root):
                raise RuntimeError("refresh changed the exact recovery directory")
            print(
                json.dumps(
                    {
                        "ok": True,
                        "session": session_name,
                        "pane_id": after.pane_id,
                        "before_pid": before.pane_pid,
                        "after_pid": after.pane_pid,
                        "exact_recovery": observed_after[1],
                        "input_echoed": "ECHO:model-refresh-probe" in after_capture,
                        "before_ready": "READY:" in before_capture,
                    },
                    sort_keys=True,
                )
            )
            return 0
        finally:
            backend._run(["kill-session", "-t", session_name], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
