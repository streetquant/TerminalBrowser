"""Static performance guards for the GTK main thread.

AST-level checks that keep slow, blocking work out of gtk_app's UI paths:
no time.sleep anywhere in the module, and subprocess use restricted to an
explicit allowlist of spawn sites that hand the child to VTE or a reader
thread instead of blocking the main loop.
"""

import ast
import pathlib
import unittest

GTK_APP = pathlib.Path(__file__).resolve().parents[1] / "termdeck" / "gtk_app.py"

# Functions allowed to create subprocesses. ``spawn_transport`` (inside
# _start_tmux_event_watcher) is consumed by the control-mode client's reader
# thread, never the GTK main loop.
SUBPROCESS_ALLOWLIST = {
    # The control-mode transport is the single subprocess the GTK app spawns:
    # pane output, redraws, and input all flow over that one tmux -C client,
    # so no other UI code path may shell out.
    "_start_tmux_event_watcher.spawn_transport",
}


def _call_sites() -> list[tuple[str, str, int]]:
    """Return (enclosing_function_path, callee_source, lineno) for every call."""

    tree = ast.parse(GTK_APP.read_text(encoding="utf-8"))
    stack: list[str] = []
    hits: list[tuple[str, str, int]] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            hits.append((".".join(stack) or "<module>", ast.unparse(node.func), node.lineno))
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


def _is_subprocess(callee: str) -> bool:
    return callee.startswith("subprocess.") or callee in {"os.system", "os.popen"}


class MainThreadPerfGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.calls = _call_sites()

    def test_no_time_sleep_in_gtk_app(self) -> None:
        sleepers = [call for call in self.calls if call[1] in {"time.sleep", "sleep"}]
        self.assertEqual(sleepers, [])

    def test_subprocess_use_is_allowlisted(self) -> None:
        subprocess_calls = [call for call in self.calls if _is_subprocess(call[1])]
        offenders = [call for call in subprocess_calls if call[0] not in SUBPROCESS_ALLOWLIST]
        self.assertEqual(offenders, [])
        # Keep the allowlist honest: every entry is still a real call site, so
        # renames force this guard to be revisited instead of rotting.
        self.assertEqual({call[0] for call in subprocess_calls}, SUBPROCESS_ALLOWLIST)

    def test_refresh_paths_never_spawn_processes(self) -> None:
        offenders = [
            call
            for call in self.calls
            if "refresh" in call[0].lower() and _is_subprocess(call[1])
        ]
        self.assertEqual(offenders, [])

    def test_no_blocking_subprocess_wrappers(self) -> None:
        blocked = {
            "subprocess.run",
            "subprocess.check_output",
            "subprocess.check_call",
            "subprocess.call",
        }
        offenders = [call for call in self.calls if call[1] in blocked]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
