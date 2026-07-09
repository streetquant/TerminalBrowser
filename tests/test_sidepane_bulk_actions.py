from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from termdeck.gtk_app import SidePane


def make_record(session_id: str):
    return SimpleNamespace(id=session_id)


class SidePaneBulkActionsTest(TestCase):
    def test_hide_records_routes_open_tabs_through_window_and_hidden_records_through_store(self) -> None:
        open_record = make_record('open')
        closed_record = make_record('closed')
        hidden_calls = []
        refresh_calls = []
        store = object()
        window = SimpleNamespace(
            tabs={'open': object()},
            app_context=SimpleNamespace(store=store),
            hide_tab=lambda session_id: hidden_calls.append(('window', session_id)),
        )
        pane = SimpleNamespace(
            window=window,
            selected_ids={'open', 'closed', 'other'},
            refresh=lambda: refresh_calls.append('refresh'),
        )

        with patch('termdeck.gtk_app.hide_records') as hide_records:
            SidePane._hide_records(pane, [open_record, closed_record])

        self.assertEqual(hidden_calls, [('window', 'open')])
        hide_records.assert_called_once_with(store, [closed_record])
        self.assertEqual(pane.selected_ids, {'other'})
        self.assertEqual(refresh_calls, ['refresh'])

    def test_hide_records_refreshes_even_for_empty_bulk_action(self) -> None:
        refresh_calls = []
        pane = SimpleNamespace(
            window=SimpleNamespace(tabs={}, app_context=SimpleNamespace(store=object()), hide_tab=lambda _sid: None),
            selected_ids={'stale'},
            refresh=lambda: refresh_calls.append('refresh'),
        )

        with patch('termdeck.gtk_app.hide_records') as hide_records:
            SidePane._hide_records(pane, [])

        hide_records.assert_not_called()
        self.assertEqual(pane.selected_ids, {'stale'})
        self.assertEqual(refresh_calls, ['refresh'])

    def test_pin_records_calls_lifecycle_helper_and_refreshes_when_records_exist(self) -> None:
        record = make_record('one')
        refresh_calls = []
        store = object()
        pane = SimpleNamespace(
            window=SimpleNamespace(app_context=SimpleNamespace(store=store)),
            refresh=lambda: refresh_calls.append('refresh'),
        )

        with patch('termdeck.gtk_app.pin_records') as pin_records:
            SidePane._pin_records(pane, [record])

        pin_records.assert_called_once_with(store, [record])
        self.assertEqual(refresh_calls, ['refresh'])

    def test_pin_records_does_not_refresh_empty_action(self) -> None:
        refresh_calls = []
        pane = SimpleNamespace(
            window=SimpleNamespace(app_context=SimpleNamespace(store=object())),
            refresh=lambda: refresh_calls.append('refresh'),
        )

        with patch('termdeck.gtk_app.pin_records') as pin_records:
            SidePane._pin_records(pane, [])

        pin_records.assert_not_called()
        self.assertEqual(refresh_calls, [])

    def test_unpin_records_calls_lifecycle_helper_and_refreshes_when_records_exist(self) -> None:
        record = make_record('one')
        refresh_calls = []
        store = object()
        pane = SimpleNamespace(
            window=SimpleNamespace(app_context=SimpleNamespace(store=store)),
            refresh=lambda: refresh_calls.append('refresh'),
        )

        with patch('termdeck.gtk_app.unpin_records') as unpin_records:
            SidePane._unpin_records(pane, [record])

        unpin_records.assert_called_once_with(store, [record])
        self.assertEqual(refresh_calls, ['refresh'])


if __name__ == '__main__':
    import unittest

    unittest.main()
