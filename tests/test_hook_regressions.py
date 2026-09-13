from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

HOOK_PATH = Path(__file__).resolve().parents[1] / "test_repair.py"


class TestRepairTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        spec = importlib.util.spec_from_file_location("repair_hook_under_test", HOOK_PATH)
        assert spec is not None and spec.loader is not None
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
        for module in self.hook.MODULES:
            (self.root / module).mkdir()
        self.hook.configure_project_root(self.root)
        self.hook._http_get = mock.Mock(
            return_value=(200, b'{"value":{"build":{"version":"2.16.2"}}}')
        )

    def write_result(
        self,
        module: str,
        *,
        full_name: str = "tests.SampleTest.testFailure",
        status: str = "failed",
        uuid: str | None = None,
        allure_id: str | None = "107",
    ) -> Path:
        target = self.root / module / "build" / "allure-results" / f"{module}-result.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        class_name, method_name = full_name.rsplit(".", 1)
        labels = [
            {"name": "testClass", "value": class_name},
            {"name": "testMethod", "value": method_name},
        ]
        if allure_id:
            labels.append({"name": "AS_ID", "value": allure_id})
        target.write_text(json.dumps({
            "uuid": uuid or f"{module}-uuid", "name": method_name,
            "fullName": full_name, "status": status, "stop": 100, "labels": labels,
        }), encoding="utf-8")
        return target

    def refresh_and_lock(self) -> dict:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, self.hook.refresh())
            self.assertEqual(0, self.hook.lock("worker"))
        return self.hook.load_queue()["items"][0]

    def event(
        self, command: str, *, name: str = "PreToolUse", output: str = "",
        exit_code: int | None = None, adapter: str = "claude",
    ) -> dict:
        return {
            "eventName": name, "adapter": self.hook.resolve_adapter(adapter),
            "command": command, "output": output, "exitCode": exit_code,
        }

    @staticmethod
    def command(item: dict) -> str:
        return f"./gradlew :{item['module']}:test --tests {item['fullName']} --rerun"

    def write_junit(
        self, item: dict, outcome: str, *, target_name: str | None = None,
        extra_cases: list[tuple[str, str, str]] | None = None,
    ) -> None:
        directory = self.root / item["module"] / "build" / "test-results" / "test"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"TEST-{item['className']}.xml"
        previous_stamp = path.stat().st_mtime_ns if path.exists() else 0
        cases = [(item["className"], target_name or item["displayName"], outcome)]
        suite = ET.Element("testsuite")
        for class_name, name, case_outcome in cases + (extra_cases or []):
            case = ET.SubElement(suite, "testcase", classname=class_name, name=name)
            if case_outcome in {"failed", "skipped"}:
                ET.SubElement(case, "failure" if case_outcome == "failed" else "skipped")
        ET.ElementTree(suite).write(path, encoding="utf-8")
        stamp = max(time.time_ns(), previous_stamp + 1)
        os.utime(path, ns=(stamp, stamp))

    def test_refresh_preserves_unfinished_repair_after_new_allure_result(self) -> None:
        self.write_result("api-tests", uuid="original-failure")
        original = self.refresh_and_lock()

        for state in ("locked", "active", "verified", "exhausted"):
            with self.subTest(state=state):
                item = dict(
                    original, state=state, attempts=3 if state == "exhausted" else 2,
                    inconclusiveRuns=1,
                )
                if state == "exhausted":
                    item["exhaustedReason"] = "repair_attempts"
                queue = self.hook.load_queue()
                queue["items"] = [item]
                self.hook.save_queue(queue)
                self.write_result("api-tests", uuid=f"new-failure-{state}", allure_id=None)

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, self.hook.refresh(if_changed=True))

                observed = self.hook.load_queue()["items"][0]
                self.assertEqual(f"new-failure-{state}", observed["resultUuid"])
                self.assertEqual(dict(item, resultUuid=observed["resultUuid"]), observed)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(1, self.hook.lock("other-worker"))

    def test_refresh_preserves_running_repair_when_results_pass_or_disappear(self) -> None:
        for result_change in ("passed", "removed"):
            with self.subTest(result_change=result_change):
                self.hook.save_queue(self.hook.empty_state())
                result = self.write_result("api-tests", uuid=f"old-{result_change}")
                item = self.refresh_and_lock()
                self.assertEqual(
                    (None, None), self.hook.process_before(self.event(self.command(item)))
                )
                running = self.hook.load_queue()["items"][0]
                if result_change == "passed":
                    self.write_result("api-tests", status="passed", uuid="passed-run")
                else:
                    result.unlink()

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, self.hook.refresh(if_changed=True))

                expected = (
                    dict(running, status="passed", resultUuid="passed-run")
                    if result_change == "passed" else running
                )
                self.assertEqual([expected], self.hook.load_queue()["items"])
                self.write_junit(item, "passed")
                self.hook.process_after(self.event(self.command(item), name="PostToolUse"))
                self.assertEqual("verified", self.hook.load_queue()["items"][0]["state"])

    def test_refresh_preserves_budgets_after_unlock_and_new_allure_result(self) -> None:
        self.write_result("api-tests", uuid="original-failure")
        item = self.refresh_and_lock()
        before = self.event(self.command(item))
        after = self.event(self.command(item), name="PostToolUse")
        self.assertEqual((None, None), self.hook.process_before(before))
        self.hook.process_after(after)
        self.assertEqual((None, None), self.hook.process_before(before))
        self.write_junit(item, "failed")
        self.hook.process_after(after)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, self.hook.unlock(item["id"]))
            self.write_result("api-tests", uuid="new-failure")
            self.assertEqual(0, self.hook.refresh(if_changed=True))
            self.assertEqual(0, self.hook.lock("next-worker"))

        resumed = self.hook.load_queue()["items"][0]
        self.assertEqual("locked", resumed["state"])
        self.assertEqual(1, resumed["attempts"])
        self.assertEqual(1, resumed["inconclusiveRuns"])
        self.assertEqual("new-failure", resumed["resultUuid"])
        self.assertEqual("next-worker", resumed["lockedBy"])

    def test_refresh_does_not_reopen_the_failure_just_completed(self) -> None:
        for outcome in ("skipped", "blocked"):
            with self.subTest(outcome=outcome):
                self.hook.save_queue(self.hook.empty_state())
                self.write_result("api-tests", uuid=f"original-{outcome}")
                item = self.refresh_and_lock()
                self.write_result("api-tests", uuid=f"observed-{outcome}")

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, self.hook.refresh())
                    self.assertEqual(0, self.hook.complete(item["id"], outcome))
                    self.assertEqual(0, self.hook.refresh())

                completed = self.hook.load_queue()["items"][0]
                self.assertEqual(outcome, completed["state"])
                self.assertEqual(f"observed-{outcome}", completed["resultUuid"])
                self.write_result("api-tests", uuid=f"later-{outcome}")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, self.hook.refresh())
                reopened = self.hook.load_queue()["items"][0]
                self.assertEqual("pending", reopened["state"])
                self.assertEqual(f"later-{outcome}", reopened["resultUuid"])

    def test_refresh_reopens_terminal_item_for_a_new_failure(self) -> None:
        self.write_result("api-tests", uuid="original-failure")
        original = self.refresh_and_lock()

        for state in ("done", "blocked", "skipped"):
            with self.subTest(state=state):
                queue = self.hook.load_queue()
                queue["items"] = [dict(original, state=state, attempts=3)]
                self.hook.save_queue(queue)
                self.write_result("api-tests", uuid=f"new-failure-{state}")

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, self.hook.refresh(if_changed=True))

                item = self.hook.load_queue()["items"][0]
                self.assertEqual("pending", item["state"])
                self.assertEqual(0, item["attempts"])
                self.assertEqual(0, item["inconclusiveRuns"])
                self.assertEqual(f"new-failure-{state}", item["resultUuid"])
                self.assertNotIn("lockedBy", item)

    def test_ordinary_commands_ignore_pending_and_active_repair_queue(self) -> None:
        commands = (
            'rg -n "test" api-tests/README.md',
            'rg -n "build" agent_docs/building_the_project.md',
            "Get-Content test",
            "echo check",
            "echo :api-tests:test --tests tests.ApiTest.testFailure --rerun",
            "echo './gradlew :api-tests:test; git status'",
            "./gradlew assemble; echo test",
            "gradle assemble",
            "gradle.bat assemble",
            'rg "test',
        )
        self.write_result("api-tests")
        with contextlib.redirect_stdout(io.StringIO()):
            self.hook.refresh()
        for state in ("pending", "locked"):
            if state == "locked":
                with contextlib.redirect_stdout(io.StringIO()):
                    self.hook.lock("worker")
            before = self.hook.STATE_PATH.read_bytes()
            for command in commands:
                with self.subTest(state=state, command=command), mock.patch.object(
                    self.hook, "state_file_lock"
                ) as state_lock:
                    self.assertFalse(self.hook.parse_gradle_command(command)["isTestCommand"])
                    self.assertEqual((None, None), self.hook.process_before(self.event(command)))
                    self.assertIsNone(self.hook.process_after(self.event(command)))
                    state_lock.assert_not_called()
            self.assertEqual(before, self.hook.STATE_PATH.read_bytes())

    def test_rejects_test_invocations_at_shell_command_boundaries(self) -> None:
        commands = (
            r".\gradlew.bat --rerun-tasks :api-tests:test; git status --short",
            "git status;./gradlew :api-tests:test",
            "git status\n./gradlew :api-tests:test",
            "echo ok # :api-tests:test\n./gradlew :api-tests:test",
            "./gradlew :api-tests:test|Out-String",
            r"& '.\gradlew.bat' :api-tests:test --tests tests.ApiTest.testFailure --rerun",
            'cmd /c ".\\gradlew.bat :api-tests:test --rerun"',
            'powershell -Command ".\\gradlew.bat :api-tests:test --rerun"',
            "bash -c './gradlew :api-tests:test --rerun'",
            "gradle :api-tests:test",
            "gradle.bat test",
        )
        self.write_result("api-tests")
        self.refresh_and_lock()
        before = self.hook.STATE_PATH.read_bytes()
        for command in commands:
            with self.subTest(command=command):
                parsed = self.hook.parse_gradle_command(command)
                self.assertTrue(parsed["isTestCommand"])
                self.assertIn("direct", parsed["error"])
                reason, _ = self.hook.process_before(self.event(command))
                self.assertIn("direct", reason)

        self.assertEqual(before, self.hook.STATE_PATH.read_bytes())

    def test_parses_exact_windows_and_bash_gradle_commands(self) -> None:
        windows = self.hook.parse_gradle_command(
            r'.\gradlew.bat :api-tests:test --tests "tests.ApiTest.testFailure" --rerun'
        )
        bash = self.hook.parse_gradle_command(
            "./gradlew :appium-tests:test --tests=tests.MapTest.testFailure --rerun-tasks"
        )

        self.assertEqual("tests.ApiTest.testFailure", windows["target"]["fullName"])
        self.assertEqual("appium-tests", bash["target"]["module"])
        self.assertTrue(windows["hasRerun"])

    def test_active_lock_allows_only_same_exact_fresh_test(self) -> None:
        self.write_result("api-tests")
        item = self.refresh_and_lock()

        reason, _ = self.hook.process_before(self.event(self.command(item)))
        other = self.command(item).replace("testFailure", "testOther")
        other_reason, _ = self.hook.process_before(self.event(other))

        self.assertIsNone(reason)
        self.assertIn("different test", other_reason)
        self.assertEqual("running", self.hook.load_queue()["items"][0]["state"])
        self.assertTrue(self.hook.STATE_PATH.exists())
        self.assertEqual(
            {"test_repair.json", "test_repair_receipts.jsonl"},
            {path.name for path in self.hook.STATE_DIR.iterdir()},
        )

    def test_active_lock_blocks_cached_or_broad_runs(self) -> None:
        self.write_result("api-tests")
        item = self.refresh_and_lock()

        cached, _ = self.hook.process_before(self.event(self.command(item).replace(" --rerun", "")))
        broad, _ = self.hook.process_before(self.event("./gradlew :api-tests:test --rerun"))

        self.assertIn("--rerun", cached)
        self.assertIn("exact", broad)
        receipts = [
            json.loads(line)
            for line in self.hook.RECEIPTS_PATH.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(["deny", "deny"], [receipt["decision"] for receipt in receipts])
        self.assertTrue(all(receipt["target"] == item["fullName"] for receipt in receipts))


if __name__ == "__main__":
    unittest.main()
