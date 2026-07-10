#!/usr/bin/env python3
"""Verify typo-tolerant Search Tabs behavior under a real GTK display."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _row_names(window) -> list[str]:
    names: list[str] = []
    iterator = window.store.get_iter_first()
    while iterator is not None:
        names.append(str(window.store.get_value(iterator, 2)))
        iterator = window.store.iter_next(iterator)
    return names


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-fuzzy-search-") as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = root / "tmux"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"

        from termdeck.gtk_app import Gtk, SessionSearchWindow, TermDeckApplication

        Gtk.init([])
        app = TermDeckApplication(db_path=root / "tabs.sqlite3")
        pytest_record = app.store.create(
            "pytest",
            "tests",
            "pytest",
            str(root),
            window_id="qa",
            window_title="Quality",
        )
        app.store.create(
            "app",
            "dev",
            "app",
            str(root),
            window_id="dev",
            window_title="Development",
        )
        build_record = app.store.create(
            "build",
            "tests",
            "make",
            str(root),
            window_id="qa",
            window_title="Quality",
        )
        app.backend.capture = lambda record, lines=120: (
            "starting build\ncompilation failed\nfinished"
            if record.id == build_record.id
            else ""
        )

        window = SessionSearchWindow(app)
        try:
            window.query.set_text("pytes")
            window.refresh()
            prefix_names = _row_names(window)
            if prefix_names != ["pytest"]:
                raise AssertionError(f"pytes did not find pytest: {prefix_names}")

            window.query.set_text("pyetst")
            window.refresh()
            transposition_names = _row_names(window)
            if transposition_names != ["pytest"]:
                raise AssertionError(
                    f"transposition typo did not find pytest: {transposition_names}"
                )

            window.query.set_text("api")
            window.refresh()
            short_query_names = _row_names(window)
            if short_query_names:
                raise AssertionError(
                    f"short query produced noisy fuzzy matches: {short_query_names}"
                )

            window.include_output.set_active(True)
            window.query.set_text("compilaton")
            window.refresh()
            output_names = _row_names(window)
            if output_names != ["build"]:
                raise AssertionError(
                    f"output typo did not find compilation capture: {output_names}"
                )
            match = next(iter(window.matches_by_id.values()))
            if match.session_id != build_record.id or match.output_match_count != 1:
                raise AssertionError("recent-output fuzzy match did not expose one snippet")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "prefix_query": "pytes",
                        "transposition_query": "pyetst",
                        "matched_tab": pytest_record.name,
                        "short_query_noise_suppressed": True,
                        "output_query": "compilaton",
                        "output_match": build_record.name,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            window.destroy()
            app.store.close()
            app.quit()


if __name__ == "__main__":
    raise SystemExit(main())
