from __future__ import annotations

from unittest import TestCase

from termdeck.history_control import (
    BackendHistoryState,
    ChildInputProtocol,
    EdgeKnowledge,
    HistoryOwner,
    ScrollDisposition,
    ScrollOutcome,
    passthrough_scroll,
    unhandled_scroll,
)


class HistoryControlTests(TestCase):
    def test_scroll_outcome_rejects_truth_value_coercion(self) -> None:
        outcome = ScrollOutcome(
            disposition=ScrollDisposition.HANDLED,
            owner=HistoryOwner.CODEX,
            edge=EdgeKnowledge.HISTORY_EXACT,
            requested_lines=-3,
            depth=3,
            managed_open=True,
        )

        with self.assertRaisesRegex(TypeError, "must not be used as a boolean"):
            bool(outcome)

    def test_live_edge_is_explicit_evidence_not_zeroish_truthiness(self) -> None:
        history = ScrollOutcome(
            disposition=ScrollDisposition.HANDLED,
            owner=HistoryOwner.TMUX,
            edge=EdgeKnowledge.HISTORY_EXACT,
            requested_lines=3,
            depth=1,
        )
        unknown = ScrollOutcome(
            disposition=ScrollDisposition.HANDLED,
            owner=HistoryOwner.TMUX,
            edge=EdgeKnowledge.UNKNOWN,
            requested_lines=3,
            depth=0,
        )
        live = ScrollOutcome(
            disposition=ScrollDisposition.HANDLED,
            owner=HistoryOwner.TMUX,
            edge=EdgeKnowledge.LIVE_EXACT,
            requested_lines=3,
            depth=0,
        )

        self.assertFalse(history.reached_live_edge)
        self.assertFalse(unknown.reached_live_edge)
        self.assertTrue(live.reached_live_edge)

    def test_child_protocol_is_rejected_outside_passthrough(self) -> None:
        with self.assertRaisesRegex(ValueError, "child input protocol"):
            ScrollOutcome(
                disposition=ScrollDisposition.HANDLED,
                owner=HistoryOwner.CHILD_TUI,
                edge=EdgeKnowledge.UNKNOWN,
                requested_lines=-3,
                child_input_protocol=ChildInputProtocol.SGR_MOUSE,
            )

    def test_backend_state_rejects_invalid_live_and_managed_combinations(self) -> None:
        with self.assertRaisesRegex(ValueError, "live history state"):
            BackendHistoryState(owner=HistoryOwner.LIVE, depth=1)
        with self.assertRaisesRegex(ValueError, "only a Codex transcript"):
            BackendHistoryState(owner=HistoryOwner.TMUX, managed_open=True)

    def test_unhandled_scroll_defaults_to_unknown_edge_evidence(self) -> None:
        outcome = unhandled_scroll(3, detail="backend query failed")

        self.assertIs(outcome.disposition, ScrollDisposition.UNHANDLED)
        self.assertIs(outcome.owner, HistoryOwner.LIVE)
        self.assertIs(outcome.edge, EdgeKnowledge.UNKNOWN)
        self.assertIsNone(outcome.depth)
        self.assertFalse(outcome.reached_live_edge)

    def test_passthrough_never_claims_a_live_edge(self) -> None:
        outcome = passthrough_scroll(
            -3,
            detail="child owns scrolling",
            protocol=ChildInputProtocol.SGR_MOUSE,
        )

        self.assertIs(outcome.disposition, ScrollDisposition.PASSTHROUGH)
        self.assertIs(outcome.owner, HistoryOwner.CHILD_TUI)
        self.assertIs(outcome.edge, EdgeKnowledge.UNKNOWN)
        self.assertFalse(outcome.reached_live_edge)
        self.assertIs(outcome.child_input_protocol, ChildInputProtocol.SGR_MOUSE)
