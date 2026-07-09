import json
import unittest

from termdeck.models import ResourceSnapshot, SessionRecord
from termdeck.resource_budget import BudgetConfigError, budget_from_mapping, budgets_from_payload, default_budgets, evaluate_budget, format_duration, parse_duration, parse_memory, render_budget_table, sample_budget_json, select_budget
from termdeck.session_store import utc_now


def record(name="codex-one", tag="ai"):
    now = utc_now()
    return SessionRecord(
        id="td-12345678",
        name=name,
        tag=tag,
        command="codex",
        cwd="/tmp",
        tmux_name=f"termdeck_{tag}_{name}",
        created_at=now,
        last_opened_at=now,
        status="running",
        last_pid=123,
        window_title="Agents",
    )


class ResourceBudgetTests(unittest.TestCase):
    def test_parse_memory_and_duration_units(self):
        self.assertEqual(parse_memory("1GiB"), 1024 ** 3)
        self.assertEqual(parse_memory("2 MB"), 2_000_000)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(format_duration(3661), "1h 01m")

    def test_invalid_budget_values_raise_clear_error(self):
        with self.assertRaises(BudgetConfigError):
            budget_from_mapping({"name": "bad", "max_memory": "5 elephants"})
        with self.assertRaises(BudgetConfigError):
            budget_from_mapping({"name": "bad", "action": "explode"})

    def test_tag_budget_overrides_global_budget(self):
        budgets = budgets_from_payload({"budgets": [
            {"name": "global", "tag": "*", "max_memory": "1GiB"},
            {"name": "ai", "tag": "ai", "max_memory": "8GiB"},
        ]})
        self.assertEqual(select_budget(record(tag="ai"), budgets).name, "ai")
        self.assertEqual(select_budget(record(tag="tests"), budgets).name, "global")

    def test_evaluate_budget_reports_multiple_violations(self):
        budget = budget_from_mapping({"name": "tight", "tag": "ai", "max_cpu_percent": 50, "max_memory": "100MiB", "max_processes": 2, "max_runtime": "1m", "action": "pause"})
        snapshot = ResourceSnapshot(pid=123, exists=True, cpu_percent=120.0, memory_bytes=200 * 1024 * 1024, runtime_seconds=130, process_count=3)
        assessment = evaluate_budget(record(), snapshot, [budget])
        self.assertTrue(assessment.over_budget)
        self.assertEqual(assessment.suggested_action, "pause")
        self.assertEqual({violation.metric for violation in assessment.violations}, {"cpu_percent", "memory_bytes", "process_count", "runtime_seconds"})
        payload = assessment.as_dict()
        self.assertEqual(payload["budget"]["name"], "tight")
        self.assertIn("CPU", payload["reason"])

    def test_sample_budget_json_is_loadable(self):
        payload = json.loads(sample_budget_json())
        budgets = budgets_from_payload(payload)
        self.assertGreaterEqual(len(budgets), 2)

    def test_render_budget_table_mentions_over_budget_reason(self):
        budget = budget_from_mapping({"name": "tight", "tag": "ai", "max_cpu_percent": 1})
        snapshot = ResourceSnapshot(pid=123, exists=True, cpu_percent=10.0, memory_bytes=1, runtime_seconds=2, process_count=1)
        table = render_budget_table([evaluate_budget(record(), snapshot, [budget])])
        self.assertIn("CPU", table)
        self.assertIn("tight", table)


if __name__ == "__main__":
    unittest.main()
