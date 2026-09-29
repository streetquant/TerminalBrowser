import os
from types import SimpleNamespace
from unittest import TestCase, mock

from termdeck.gtk_app import TermDeckApplication
from termdeck.workspace_recipe import RecipeTab, RecipeWindow


class StoreStub:
    def __init__(self) -> None:
        self.created = []
        self.record = None

    def create(self, **values):
        self.created.append(values)
        self.record = SimpleNamespace(id="session-1", last_pid=None, **values)
        return self.record

    def update(self, session_id: str, **changes):
        assert self.record is not None
        assert session_id == self.record.id
        values = vars(self.record) | changes
        self.record = SimpleNamespace(**values)
        return self.record


class ProjectPaneRuntimeTest(TestCase):
    def test_open_recipe_window_materialises_shell_placeholder_before_launch(self) -> None:
        store = StoreStub()
        backend = SimpleNamespace(
            create_or_resume=lambda record: None,
            pane_pid=lambda record: 4321,
        )
        side = SimpleNamespace(refresh=lambda: None)
        window = SimpleNamespace(
            side=side,
            show_all=lambda: None,
            open_session=lambda *args, **kwargs: None,
        )
        app = SimpleNamespace(store=store, backend=backend, windows=[])
        recipe = RecipeWindow(
            title="Development",
            tabs=(
                RecipeTab(
                    name="shell",
                    tag="default",
                    command="{shell}",
                    cwd="/tmp/project",
                ),
                RecipeTab(
                    name="project",
                    tag="default",
                    command="{shell}",
                    cwd="/tmp/project",
                    automatic_name=True,
                ),
            ),
        )

        with mock.patch("termdeck.gtk_app.TermDeckWindow", return_value=window):
            TermDeckApplication._open_recipe_window(app, recipe, "demo", 1)

        self.assertEqual(store.created[0]["command"], os.environ.get("SHELL", "/bin/sh"))
        self.assertNotIn("{shell}", store.created[0]["command"])
        self.assertEqual([record["automatic_name"] for record in store.created], [False, True])


if __name__ == "__main__":
    import unittest

    unittest.main()
