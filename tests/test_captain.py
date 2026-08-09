import json
import shlex
from pathlib import Path
from unittest import TestCase

from termdeck.captain import DEFAULT_ROSTER_LIMIT, build_captain_envelope
from termdeck.models import SessionRecord


def record(
    session_id: str,
    *,
    window_id: str = "window-a",
    tab_index: int = 0,
    visible: bool = True,
    status: str = "running",
    name: str | None = None,
    cwd: str = "/work",
    command: str = "bash",
) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name=name or session_id,
        tag="work",
        command=command,
        cwd=cwd,
        tmux_name=f"termdeck-{session_id}",
        created_at="2026-08-09T00:00:00Z",
        last_opened_at="2026-08-09T00:00:00Z",
        status=status,
        window_id=window_id,
        window_title="Window A",
        tab_index=tab_index,
        visible=visible,
    )


def roster_from(envelope: str) -> list[dict[str, object]]:
    prefix = "Crew roster (canonical JSON data): "
    line = next(row for row in envelope.splitlines() if row.startswith(prefix))
    return json.loads(line.removeprefix(prefix))


class CaptainEnvelopeTests(TestCase):
    def build(self, rows, **overrides) -> str:
        values = {
            "window_id": "window-a",
            "window_title": "Window A",
            "captain": record("captain", tab_index=0, command="codex"),
            "records": rows,
            "user_request": "Coordinate the work.",
            "database_path": Path("/tmp/termdeck.sqlite3"),
        }
        values.update(overrides)
        return build_captain_envelope(**values)

    def test_roster_is_window_local_visible_live_and_excludes_captain(self) -> None:
        captain = record("captain", tab_index=0, command="codex")
        envelope = self.build(
            [
                record("other-window", window_id="window-b"),
                record("hidden", visible=False),
                record("stopped", status="STOPPED"),
                record("archived", status="archived"),
                captain,
                record("crew", tab_index=3),
            ],
            captain=captain,
        )

        self.assertEqual([row["id"] for row in roster_from(envelope)], ["crew"])

    def test_roster_order_is_stable_by_tab_index_then_id(self) -> None:
        rows = [
            record("zeta", tab_index=2),
            record("zulu", tab_index=1),
            record("alpha", tab_index=1),
        ]

        envelope = self.build(reversed(rows))

        self.assertEqual([row["id"] for row in roster_from(envelope)], ["alpha", "zulu", "zeta"])

    def test_metadata_is_canonical_json_data_and_round_trips(self) -> None:
        dangerous = record(
            "crew-safe-id",
            name='name "quoted"\n$(touch /tmp/nope)',
            cwd="/work/it's here",
            command="printf '%s' \"$TOKEN\" && `false`",
        )

        row = roster_from(self.build([dangerous]))[0]

        self.assertEqual(row["name"], dangerous.name)
        self.assertEqual(row["cwd"], dangerous.cwd)
        self.assertEqual(row["command"], dangerous.command)

    def test_individual_metadata_fields_are_bounded(self) -> None:
        oversized = record(
            "crew",
            name="n" * 400,
            cwd="/" + "p" * 2000,
            command="x" * 2000,
        )

        row = roster_from(self.build([oversized]))[0]

        self.assertEqual(len(row["name"]), 256)
        self.assertEqual(len(row["cwd"]), 1024)
        self.assertEqual(len(row["command"]), 1024)
        self.assertTrue(row["name"].endswith("…"))

    def test_user_request_is_embedded_verbatim_without_stripping(self) -> None:
        request = "  first line\nsecond line\n"

        envelope = self.build([], user_request=request)

        start = f"----- BEGIN USER REQUEST (verbatim; characters={len(request)}) -----\n"
        framed = envelope.split(start, 1)[1]
        self.assertEqual(framed[: len(request)], request)
        self.assertTrue(framed[len(request) :].startswith("\n----- END USER REQUEST -----"))

    def test_command_context_shell_quotes_window_and_database_path(self) -> None:
        window_id = "window 'alpha beta'"
        database_path = Path("/tmp/db dir/it's.sqlite3")
        captain = record("captain", window_id=window_id, command="codex")

        envelope = self.build(
            [],
            window_id=window_id,
            captain=captain,
            database_path=database_path,
        )

        for command in ("sessions", "capture", "control-plan", "send"):
            line = next(row for row in envelope.splitlines() if row.startswith(f"t1000 {command}"))
            self.assertIn(f"--window-id {shlex.quote(window_id)}", line)
            self.assertIn(f"--db {shlex.quote(str(database_path))}", line)
            self.assertNotIn("$TERMDECK_", line)

    def test_relative_database_path_becomes_stable_for_other_tab_directories(self) -> None:
        envelope = self.build([], database_path=Path("state/termdeck.sqlite3"))

        expected = str(Path.cwd() / "state" / "termdeck.sqlite3")
        self.assertIn(f"--db {shlex.quote(expected)}", envelope)

    def test_default_roster_bound_reports_omissions(self) -> None:
        rows = [record(f"crew-{index:03d}", tab_index=index) for index in range(DEFAULT_ROSTER_LIMIT + 6)]

        envelope = self.build(rows)

        self.assertEqual(len(roster_from(envelope)), DEFAULT_ROSTER_LIMIT)
        self.assertIn(
            f"Crew roster: {DEFAULT_ROSTER_LIMIT} included of {DEFAULT_ROSTER_LIMIT + 6} eligible; "
            f"limit={DEFAULT_ROSTER_LIMIT}; omitted=6.",
            envelope,
        )

    def test_explicit_roster_bound_is_applied_after_stable_sort(self) -> None:
        envelope = self.build(
            [record("third", tab_index=3), record("first", tab_index=1), record("second", tab_index=2)],
            roster_limit=2,
        )

        self.assertEqual([row["id"] for row in roster_from(envelope)], ["first", "second"])
        self.assertIn("limit=2; omitted=1", envelope)

    def test_prompt_forbids_automatic_or_raw_capture_and_keeps_confirmation(self) -> None:
        envelope = self.build([record("crew")])

        self.assertIn("Never capture terminal output automatically.", envelope)
        self.assertIn("do not use --raw", envelope)
        self.assertIn("Review control-plan before sending", envelope)
        send_line = next(row for row in envelope.splitlines() if row.startswith("t1000 send"))
        self.assertNotIn("--yes", send_line)

    def test_rejects_captain_from_another_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "captain must belong"):
            self.build([], captain=record("captain", window_id="window-b", command="codex"))

    def test_rejects_non_positive_roster_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "roster_limit"):
            self.build([], roster_limit=0)


if __name__ == "__main__":
    import unittest

    unittest.main()
