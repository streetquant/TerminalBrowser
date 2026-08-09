from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import SessionSearchWindow, TermDeckApplication, TermDeckWindow


class WindowOwnerRoutingTests(TestCase):
    def test_window_lookup_uses_logical_window_id(self) -> None:
        first = SimpleNamespace(window_id="first")
        second = SimpleNamespace(window_id="second")
        app = SimpleNamespace(windows=[first, second])

        self.assertIs(TermDeckApplication.window_for_window_id(app, "second"), second)
        self.assertIsNone(TermDeckApplication.window_for_window_id(app, "missing"))

    def test_search_result_opens_in_its_owner_instead_of_last_window(self) -> None:
        calls = []
        record = SimpleNamespace(id="tab-1", window_id="owner")
        owner = SimpleNamespace(window_id="owner", open_session=lambda row: calls.append(("owner", row.id)))
        other = SimpleNamespace(window_id="other", open_session=lambda row: calls.append(("other", row.id)))
        app = SimpleNamespace(
            windows=[owner, other],
            store=SimpleNamespace(get=lambda _session_id: record),
        )
        app.window_for_window_id = lambda window_id: TermDeckApplication.window_for_window_id(app, window_id)
        search = SimpleNamespace(
            app_context=app,
            selected_match=lambda: SimpleNamespace(session_id=record.id),
        )

        SessionSearchWindow.open_selected(search)

        self.assertEqual(calls, [("owner", "tab-1")])

    def test_palette_result_opens_in_its_owner(self) -> None:
        calls = []
        record = SimpleNamespace(id="tab-1", window_id="owner")
        owner = SimpleNamespace(window_id="owner", open_session=lambda row: calls.append(("owner", row.id)))
        invoking = SimpleNamespace(
            app_context=SimpleNamespace(
                store=SimpleNamespace(get=lambda _session_id: record),
                window_for_window_id=lambda _window_id: owner,
            ),
            open_session=lambda row: calls.append(("invoking", row.id)),
        )
        item = SimpleNamespace(kind="session", payload=record.id)

        TermDeckWindow.execute_palette_item(invoking, item)

        self.assertEqual(calls, [("owner", "tab-1")])


if __name__ == "__main__":
    import unittest

    unittest.main()
