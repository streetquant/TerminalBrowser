"""Bounded concurrency tests for containment-resize notification dispatch."""

from __future__ import annotations

import threading
import time

from termdeck.pane_stream import ResizeSignalDispatcher


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def test_latest_resize_coalesces_while_one_callback_is_active() -> None:
    dispatcher = ResizeSignalDispatcher(max_pending=8, history_limit=16)
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[int, int, int]] = []

    def callback(pid: int, columns: int, rows: int) -> None:
        calls.append((pid, columns, rows))
        if len(calls) == 1:
            entered.set()
            assert release.wait(3.0)

    try:
        assert dispatcher.submit_latest(
            callback, pane_root_pid=4242, columns=80, rows=24
        )
        assert entered.wait(1.0)
        for columns in (81, 82, 83, 84):
            assert dispatcher.submit_latest(
                callback,
                pane_root_pid=4242,
                columns=columns,
                rows=24,
            )
        snapshot = dispatcher.snapshot()
        assert snapshot["pending"] == 1
        release.set()
        wait_until(lambda: len(calls) == 2)
        assert calls == [(4242, 80, 24), (4242, 84, 24)]
        wait_until(lambda: dispatcher.snapshot()["pending"] == 0)
    finally:
        release.set()
        assert dispatcher.shutdown(1.0)


def test_dispatch_capacity_is_finite_and_key_isolation_is_preserved() -> None:
    dispatcher = ResizeSignalDispatcher(max_pending=2, history_limit=16)
    entered = threading.Event()
    release = threading.Event()

    def callback(_pid: int, _columns: int, _rows: int) -> None:
        entered.set()
        assert release.wait(3.0)

    try:
        assert dispatcher.submit_latest(
            callback, pane_root_pid=100, columns=80, rows=24
        )
        assert entered.wait(1.0)
        assert dispatcher.submit_latest(
            callback, pane_root_pid=101, columns=81, rows=24
        )
        assert dispatcher.submit_latest(
            callback, pane_root_pid=102, columns=82, rows=24
        )
        assert not dispatcher.submit_latest(
            callback, pane_root_pid=103, columns=83, rows=24
        )
        assert dispatcher.snapshot()["pending"] == 2
    finally:
        release.set()
        assert dispatcher.shutdown(1.0)



def test_barrier_overtakes_queued_latest_work() -> None:
    dispatcher = ResizeSignalDispatcher(max_pending=8, history_limit=16)
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def active(_pid: int, _columns: int, _rows: int) -> None:
        order.append("active")
        entered.set()
        assert release.wait(3.0)

    def ordinary(pid: int, _columns: int, _rows: int) -> None:
        order.append(f"ordinary-{pid}")

    def barrier(_pid: int, _columns: int, _rows: int) -> None:
        order.append("barrier")

    try:
        assert dispatcher.submit_latest(
            active, pane_root_pid=150, columns=80, rows=24
        )
        assert entered.wait(1.0)
        assert dispatcher.submit_latest(
            ordinary, pane_root_pid=151, columns=81, rows=24
        )
        assert dispatcher.submit_latest(
            ordinary, pane_root_pid=152, columns=82, rows=24
        )
        ticket = dispatcher.submit_barrier(
            barrier, pane_root_pid=153, columns=83, rows=24
        )
        assert ticket is not None
        release.set()
        assert ticket.wait(1.0)
        wait_until(lambda: len(order) == 4)
        assert order[0] == "active"
        assert order[1] == "barrier"
        assert set(order[2:]) == {"ordinary-151", "ordinary-152"}
    finally:
        release.set()
        assert dispatcher.shutdown(1.0)

def test_barrier_exception_completes_failure_and_worker_survives() -> None:
    dispatcher = ResizeSignalDispatcher(max_pending=8, history_limit=16)

    def broken(_pid: int, _columns: int, _rows: int) -> None:
        raise RuntimeError("injected private value must not leak: secret-value")

    calls: list[tuple[int, int, int]] = []

    def healthy(pid: int, columns: int, rows: int) -> None:
        calls.append((pid, columns, rows))

    try:
        failed = dispatcher.submit_barrier(
            broken, pane_root_pid=200, columns=90, rows=30
        )
        assert failed is not None
        assert failed.wait(1.0)
        assert failed.completed
        assert failed.succeeded is False
        assert "RuntimeError" in failed.error
        assert len(failed.error) <= 512

        passed = dispatcher.submit_barrier(
            healthy, pane_root_pid=201, columns=91, rows=31
        )
        assert passed is not None
        assert passed.wait(1.0)
        assert calls == [(201, 91, 31)]
        snapshot = dispatcher.snapshot()
        assert snapshot["thread_alive"] is True
        history_text = repr(snapshot["history"])
        assert "secret-value" not in history_text
    finally:
        assert dispatcher.shutdown(1.0)



def test_cancelled_pending_barrier_never_invokes_callback() -> None:
    dispatcher = ResizeSignalDispatcher(max_pending=8, history_limit=16)
    entered = threading.Event()
    release = threading.Event()
    barrier_calls: list[tuple[int, int, int]] = []

    def blocking(_pid: int, _columns: int, _rows: int) -> None:
        entered.set()
        assert release.wait(3.0)

    try:
        assert dispatcher.submit_latest(
            blocking, pane_root_pid=250, columns=80, rows=24
        )
        assert entered.wait(1.0)
        ticket = dispatcher.submit_barrier(
            lambda pid, columns, rows: barrier_calls.append((pid, columns, rows)),
            pane_root_pid=251,
            columns=81,
            rows=25,
        )
        assert ticket is not None
        assert ticket.cancel("resize notification timed out")
        assert ticket.completed
        assert ticket.cancelled
        assert ticket.succeeded is False
        release.set()
        wait_until(
            lambda: dispatcher.snapshot()["pending"] == 0
            and dispatcher.snapshot()["active"] is False
        )
        assert barrier_calls == []
    finally:
        release.set()
        assert dispatcher.shutdown(1.0)

def test_shutdown_releases_pending_barrier_and_is_idempotent() -> None:
    dispatcher = ResizeSignalDispatcher(max_pending=8, history_limit=16)
    entered = threading.Event()
    release = threading.Event()

    def blocking(_pid: int, _columns: int, _rows: int) -> None:
        entered.set()
        assert release.wait(3.0)

    try:
        assert dispatcher.submit_latest(
            blocking, pane_root_pid=300, columns=100, rows=40
        )
        assert entered.wait(1.0)
        pending = dispatcher.submit_barrier(
            lambda *_args: None,
            pane_root_pid=301,
            columns=101,
            rows=41,
        )
        assert pending is not None
        assert dispatcher.shutdown(0.01) is False
        assert pending.completed
        assert pending.wait(0.0)
        assert pending.succeeded is False
        assert "shutdown" in pending.error
    finally:
        release.set()
        wait_until(lambda: dispatcher.shutdown(0.05), timeout=2.0)
        assert dispatcher.shutdown(0.0) is True


def test_multithreaded_latest_submissions_remain_bounded() -> None:
    dispatcher = ResizeSignalDispatcher(max_pending=32, history_limit=32)
    errors: list[BaseException] = []
    calls = 0
    calls_lock = threading.Lock()

    def callback(_pid: int, _columns: int, _rows: int) -> None:
        nonlocal calls
        with calls_lock:
            calls += 1

    def submitter(offset: int) -> None:
        try:
            for index in range(300):
                dispatcher.submit_latest(
                    callback,
                    pane_root_pid=400 + ((index + offset) % 8),
                    columns=80 + (index % 20),
                    rows=24 + (index % 8),
                )
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    threads = [threading.Thread(target=submitter, args=(index,)) for index in range(8)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
            assert not thread.is_alive()
        assert errors == []
        wait_until(
            lambda: dispatcher.snapshot()["pending"] == 0
            and dispatcher.snapshot()["active"] is False,
            timeout=5.0,
        )
        snapshot = dispatcher.snapshot()
        assert snapshot["pending"] == 0
        assert len(snapshot["history"]) <= 32
        assert calls > 0
    finally:
        assert dispatcher.shutdown(1.0)
