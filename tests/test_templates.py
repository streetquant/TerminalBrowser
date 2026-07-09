import os
import unittest

from termdeck.templates import get_template, search_templates, split_command


class TemplateTests(unittest.TestCase):
    def test_search_finds_codex_template(self):
        rows = search_templates("codex")
        self.assertTrue(rows)
        self.assertEqual(rows[0].id, "codex")

    def test_shell_template_materialises_default_shell_and_cwd(self):
        template = get_template("shell")
        self.assertIsNotNone(template)
        name, tag, command, cwd = template.materialise("/tmp")  # type: ignore[union-attr]
        self.assertEqual(name, "shell")
        self.assertEqual(tag, "default")
        self.assertEqual(command, os.environ.get("SHELL", "/bin/sh"))
        self.assertEqual(cwd, "/tmp")

    def test_split_command_handles_quotes(self):
        self.assertEqual(split_command('python -c "print(1)"'), ["python", "-c", "print(1)"])


if __name__ == "__main__":
    unittest.main()
