import unittest

from termdeck.task_templates import render_command, serialise_statuses, template_by_id, template_statuses


class TaskTemplateTests(unittest.TestCase):
    def test_shell_template_renders_shell_placeholder(self):
        template = template_by_id("shell")
        self.assertEqual(render_command(template.command, shell="/bin/zsh"), "/bin/zsh")

    def test_missing_dependency_is_reported_without_hiding_template(self):
        statuses = template_statuses(which=lambda _name: None)
        codex = next(status for status in statuses if status.template.id == "codex")
        custom = next(status for status in statuses if status.template.id == "custom")
        self.assertFalse(codex.available)
        self.assertEqual(codex.missing, ("codex",))
        self.assertTrue(custom.available)

    def test_serialised_statuses_are_json_ready(self):
        payload = serialise_statuses(template_statuses(which=lambda name: f"/usr/bin/{name}"))
        first = payload[0]
        self.assertIn("id", first)
        self.assertIn("available", first)
        self.assertTrue(all(isinstance(row["missing"], list) for row in payload))


if __name__ == "__main__":
    unittest.main()
