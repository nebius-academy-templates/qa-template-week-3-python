#!/usr/bin/env python3
"""Shared exact-test repair queue and shell guard for Codex and Claude Code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import socket
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MODULES = ("appium-tests", "api-tests")


def default_project_root() -> Path:
    script_directory = Path(__file__).resolve().parent
    for candidate in (script_directory, *script_directory.parents):
        if any((candidate / module).is_dir() for module in MODULES):
            return candidate
    return script_directory


REPO_ROOT = default_project_root()
STATE_DIR = REPO_ROOT / ".agent-state"
STATE_PATH = STATE_DIR / "test_repair.json"
STATE_FILE_LOCK_PATH = STATE_DIR / "test_repair.lock"
RECEIPTS_PATH = STATE_DIR / "test_repair_receipts.jsonl"

FAILED_STATUSES = {"failed", "broken"}
TERMINAL_STATES = {"done", "blocked", "skipped"}
LOCK_TTL = timedelta(hours=2)
MAX_REPAIR_ATTEMPTS = 3
MAX_INCONCLUSIVE_RUNS = 3

# The dialect is supplied by the client configuration, never guessed from the
# payload: a malformed event must still be answered in the caller's format.
ADAPTERS = ("codex", "claude")

TEST_TASKS = {
    ":api-tests:test": "api-tests",
    "api-tests:test": "api-tests",
    ":appium-tests:test": "appium-tests",
    "appium-tests:test": "appium-tests",
}
AGGREGATE_TEST_TASK_NAMES = frozenset({"test", "check", "build"})
ACTIVE_STATES = frozenset({"locked", "active", "running", "verified", "exhausted"})
GRADLE_WRAPPERS = frozenset({"gradlew", "gradlew.bat"})
SHELL_CONTROL_FRAGMENTS = ("\r", "\n", "&&", "||", ";", "|", "&", ">", "<", "`", "$(")
INFRASTRUCTURE_MARKERS = (
    "sessionnotcreatedexception",
    "uiautomation not connected",
    "uiautomator2 server",
    "device offline",
    "no devices/emulators found",
    "eacces: permission denied",
    "permissionerror: [errno 13]",
    "accessdeniedexception",
    "stream has been aborted",
)


def resolve_project_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise argparse.ArgumentTypeError(f"project root is not a directory: {root}")
    if not any((root / module).is_dir() for module in MODULES):
        modules = " or ".join(MODULES)
        raise argparse.ArgumentTypeError(
            f"project root must contain {modules}: {root}"
        )
    return root


def configure_project_root(root: Path) -> None:
    global REPO_ROOT, STATE_DIR, STATE_PATH, STATE_FILE_LOCK_PATH, RECEIPTS_PATH
    REPO_ROOT = root
    STATE_DIR = root / ".agent-state"
    STATE_PATH = STATE_DIR / "test_repair.json"
    STATE_FILE_LOCK_PATH = STATE_DIR / "test_repair.lock"
    RECEIPTS_PATH = STATE_DIR / "test_repair_receipts.jsonl"


def now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@contextmanager
def state_file_lock(timeout_seconds: float = 10.0) -> Iterator[None]:
    """Serialize repair-state mutations without third-party dependencies."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            descriptor = os.open(
                STATE_FILE_LOCK_PATH,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            try:
                os.write(descriptor, f"{os.getpid()}\n".encode())
            except OSError:
                STATE_FILE_LOCK_PATH.unlink(missing_ok=True)
                raise
            finally:
                os.close(descriptor)
            break
        except FileExistsError:
            try:
                stale = time.time() - STATE_FILE_LOCK_PATH.stat().st_mtime > 60
            except FileNotFoundError:
                continue
            if stale:
                STATE_FILE_LOCK_PATH.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Test-repair state file is busy: {STATE_FILE_LOCK_PATH}"
                ) from None
            time.sleep(0.05)
    try:
        yield
    finally:
        STATE_FILE_LOCK_PATH.unlink(missing_ok=True)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return value


def _write_json_object(path: Path, value: dict[str, Any] | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if value is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "enabled": True,
        "updatedAt": now_iso(),
        "sourceFingerprint": "",
        "items": [],
    }


def load_queue() -> dict[str, Any]:
    return _read_json_object(STATE_PATH) or empty_state()


def save_queue(state: dict[str, Any]) -> None:
    state["updatedAt"] = now_iso()
    _write_json_object(STATE_PATH, state)


def source_files() -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for module in MODULES:
        module_root = REPO_ROOT / module
        raw = list((module_root / "build" / "allure-results").glob("*-result.json"))
        if raw:
            files.extend((module, path) for path in raw)
            continue
        for report_dir in ("test-cases", "test-results"):
            report = module_root / "build" / "reports" / "allure-report" / "data" / report_dir
            files.extend((module, path) for path in report.glob("*.json"))
    return files


def source_fingerprint(files: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for module, path in sorted(files, key=lambda pair: str(pair[1])):
        relative_path = path.relative_to(REPO_ROOT).as_posix()
        digest.update(f"{module}:{relative_path}\0".encode())
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    digest.update(chunk)
        except OSError as error:
            raise RuntimeError(f"Cannot fingerprint Allure result {path}: {error}") from error
    return digest.hexdigest()


def _label_value(result: dict[str, Any], name: str) -> str | None:
    for label in result.get("labels", []):
        if isinstance(label, dict) and label.get("name") == name:
            value = label.get("value")
            return value if isinstance(value, str) else None
    return None


def normalize_result(module: str, path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid Allure result JSON {path}: {error}") from error
    except OSError as error:
        raise RuntimeError(f"Cannot read Allure result {path}: {error}") from error
    if not isinstance(result, dict):
        raise RuntimeError(f"Allure result must be a JSON object: {path}")
    status = str(result.get("status", "")).lower()
    full_name = result.get("fullName") or result.get("testCaseName")
    if not isinstance(full_name, str) or "." not in full_name:
        raise RuntimeError(f"Allure result has no qualified test identity: {path}")
    class_name = _label_value(result, "testClass") or _label_value(result, "suite")
    method_name = _label_value(result, "testMethod")
    class_name = class_name or full_name.rsplit(".", 1)[0]
    method_name = method_name or full_name.rsplit(".", 1)[1]
    stopped_at = int(result.get("stop") or path.stat().st_mtime_ns // 1_000_000)
    details = result.get("statusDetails") or result.get("error") or {}
    details = details if isinstance(details, dict) else {}
    raw_message = str(details.get("message") or "")
    message_lines = [line for line in raw_message.splitlines() if line.strip()]
    key = f"{module}:{full_name}"
    allure_id = _label_value(result, "AS_ID") or _label_value(result, "allureId")
    return {
        "id": hashlib.sha1(key.encode()).hexdigest()[:12],
        "key": key,
        "module": module,
        "className": class_name,
        "methodName": method_name,
        "displayName": result.get("name") or result.get("testCaseName") or full_name,
        "fullName": full_name,
        "allureId": allure_id,
        "status": status,
        "message": message_lines[0][:500] if message_lines else "",
        "resultUuid": result.get("uuid") or path.stem,
        "source": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "stoppedAt": stopped_at,
    }


def refresh(if_changed: bool = False) -> int:
    files = source_files()
    fingerprint = source_fingerprint(files)
    with state_file_lock():
        queue = load_queue()
        if if_changed and queue.get("sourceFingerprint") == fingerprint:
            return 0
        newest: dict[str, dict[str, Any]] = {}
        for module, path in files:
            result = normalize_result(module, path)
            previous = newest.get(result["key"])
            if previous is None or result["stoppedAt"] > previous["stoppedAt"]:
                newest[result["key"]] = result

        existing = {item.get("key"): item for item in queue.get("items", [])}
        items: list[dict[str, Any]] = []
        for result in newest.values():
            if result["status"] not in FAILED_STATUSES:
                continue
            previous = existing.get(result["key"])
            if isinstance(previous, dict) and previous.get("resultUuid") == result["resultUuid"]:
                for field in (
                    "state",
                    "attempts",
                    "inconclusiveRuns",
                    "exhaustedReason",
                    "lockedBy",
                    "lockedAt",
                    "runStartedAt",
                    "junitSnapshot",
                    "outcome",
                    "reason",
                    "completedAt",
                    "pendingNotice",
                ):
                    if field in previous:
                        result[field] = previous[field]
                result["allureId"] = result.get("allureId") or previous.get("allureId")
            else:
                result["state"] = "pending"
                result["attempts"] = 0
                result["inconclusiveRuns"] = 0
            items.append(result)
        items.sort(key=lambda item: (item["module"], item["stoppedAt"], item["key"]))
        queue["sourceFingerprint"] = fingerprint
        queue["items"] = items
        save_queue(queue)
    print(f"Queue refreshed: {len(items)} failed/broken test(s)")
    return 0


def _unlock_stale_items(items: list[dict[str, Any]]) -> bool:
    cutoff = now() - LOCK_TTL
    changed = False
    for item in items:
        locked_at = parse_iso(item.get("lockedAt"))
        if (
            item.get("state") in {"locked", "active", "running"}
            and locked_at
            and locked_at < cutoff
        ):
            item["state"] = "pending"
            for field in (
                "lockedBy",
                "lockedAt",
                "runStartedAt",
                "junitSnapshot",
                "pendingNotice",
            ):
                item.pop(field, None)
            changed = True
    return changed


def lock(worker: str | None = None) -> int:
    with state_file_lock():
        queue = load_queue()
        items = queue.get("items", [])
        _unlock_stale_items(items)
        active = [
            item
            for item in items
            if isinstance(item, dict) and item.get("state") in ACTIVE_STATES
        ]
        if active:
            save_queue(queue)
            print(
                f"Repair lock is held by {active[0].get('id', '<unknown>')}. "
                "Unlock or complete it before locking another failure.",
                file=sys.stderr,
            )
            return 1
        pending = next(
            (item for item in items if isinstance(item, dict) and item.get("state") == "pending"),
            None,
        )
        if pending is None:
            save_queue(queue)
            print("Queue empty: no pending failed tests")
            return 0
        pending["state"] = "locked"
        pending["lockedBy"] = worker or f"{socket.gethostname()}:{os.getpid()}"
        pending["lockedAt"] = now_iso()
        pending.setdefault("attempts", 0)
        pending.setdefault("inconclusiveRuns", 0)
        save_queue(queue)
        print(json.dumps(pending, ensure_ascii=False, indent=2))
    return 0


def complete(item_id: str, outcome: str, reason: str | None = None) -> int:
    with state_file_lock():
        queue = load_queue()
        item = next(
            (candidate for candidate in queue.get("items", []) if candidate.get("id") == item_id),
            None,
        )
        if item is None:
            print(f"Unknown queue id: {item_id}", file=sys.stderr)
            return 1
        state = item.get("state")
        if outcome == "fixed" and state != "verified":
            print("Fixed completion requires fresh verified JUnit proof.", file=sys.stderr)
            return 1
        if outcome != "fixed" and state not in {"locked", "active", "exhausted", "verified"}:
            print(f"Queue item is not active: {item_id}", file=sys.stderr)
            return 1
        item["state"] = "done" if outcome == "fixed" else outcome
        item["outcome"] = outcome
        item["completedAt"] = now_iso()
        if reason:
            item["reason"] = reason
        save_queue(queue)
        print(f"{item_id}: {outcome}")
    return 0


def unlock(item_id: str) -> int:
    with state_file_lock():
        queue = load_queue()
        item = next(
            (candidate for candidate in queue.get("items", []) if candidate.get("id") == item_id),
            None,
        )
        if item is None:
            print(f"Unknown queue id: {item_id}", file=sys.stderr)
            return 1
        if item.get("state") not in {"locked", "active", "running"}:
            print(f"Queue item cannot be unlocked from state {item.get('state')}", file=sys.stderr)
            return 1
        item["state"] = "pending"
        for field in (
            "lockedBy",
            "lockedAt",
            "runStartedAt",
            "junitSnapshot",
            "pendingNotice",
        ):
            item.pop(field, None)
        save_queue(queue)
        print(f"{item_id}: unlocked")
    return 0


def show() -> int:
    queue = load_queue()
    items = queue.get("items", [])
    if not items:
        print("Queue empty")
        return 0
    for item in items:
        print(
            f"{item['id']}  {item.get('state', 'pending'):10}  "
            f"{item['module']:12}  {item['fullName']}"
        )
    return 0


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _command_tokens(command: str) -> tuple[list[str], str | None]:
    try:
        raw_tokens = shlex.split(command, posix=False)
    except ValueError as error:
        return [], f"Cannot parse shell command safely: {error}"
    tokens: list[str] = []
    for token in raw_tokens:
        if token.startswith("#"):
            break
        tokens.append(token)
    return tokens, None


def _is_test_running_task(token: str) -> bool:
    """Return whether a Gradle task can transitively execute tests."""
    task_name = _unquote(token).rstrip(":").rsplit(":", 1)[-1]
    return task_name in AGGREGATE_TEST_TASK_NAMES


def parse_gradle_command(command: str) -> dict[str, Any]:
    tokens, parse_error = _command_tokens(command)
    if parse_error:
        rough_tokens = {
            token.strip("\"'();&|")
            for token in command.split()
        }
        is_test_command = any(_is_test_running_task(token) for token in rough_tokens)
        return (
            {"isTestCommand": True, "error": parse_error}
            if is_test_command
            else {"isTestCommand": False}
        )
    modules = {TEST_TASKS[token] for token in tokens if token in TEST_TASKS}
    aggregate_tasks = [
        token
        for token in tokens
        if token not in TEST_TASKS and _is_test_running_task(token)
    ]
    if not modules and not aggregate_tasks:
        return {"isTestCommand": False}
    if any(fragment in command for fragment in SHELL_CONTROL_FRAGMENTS):
        return {
            "isTestCommand": True,
            "error": (
                "Use one direct Gradle invocation; shell operators, redirections, "
                "substitutions, and multiline commands are blocked."
            ),
        }
    executable = _unquote(tokens[0]).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable not in GRADLE_WRAPPERS:
        return {
            "isTestCommand": True,
            "error": "Run the test with one direct repository Gradle wrapper invocation.",
        }
    if aggregate_tasks:
        return {
            "isTestCommand": True,
            "error": (
                "An aggregate Gradle task can run multiple tests. Run one exact "
                "module test with --tests Class.method instead."
            ),
        }
    if len(modules) != 1:
        return {"isTestCommand": True, "error": "Run one test module at a time."}
    tasks = [token for token in tokens if token in TEST_TASKS]
    if len(tasks) != 1:
        return {"isTestCommand": True, "error": "Run exactly one Gradle test task."}
    filters: list[str] = []
    rerun = any(token in {"--rerun", "--rerun-tasks"} for token in tokens)
    consumed_values: set[int] = set()
    for index, token in enumerate(tokens):
        if token == "--tests":
            if index + 1 >= len(tokens):
                return {
                    "isTestCommand": True,
                    "hasRerun": rerun,
                    "error": "The --tests option requires a value.",
                }
            consumed_values.add(index + 1)
            filters.append(_unquote(tokens[index + 1]))
        elif token.startswith("--tests="):
            filters.append(_unquote(token.removeprefix("--tests=")))
    for index, token in enumerate(tokens[1:], start=1):
        if index in consumed_values or token in TEST_TASKS or token == "--tests":
            continue
        if token.startswith("-"):
            continue
        return {
            "isTestCommand": True,
            "hasRerun": rerun,
            "error": "Use one direct Gradle invocation with one test task.",
        }
    if len(filters) > 1:
        return {"isTestCommand": True, "hasRerun": rerun, "error": "Use one --tests filter."}
    if not filters:
        return {"isTestCommand": True, "hasRerun": rerun}
    target = filters[0]
    parts = target.split(".")
    if len(parts) < 3 or not all(part.isidentifier() for part in parts):
        return {
            "isTestCommand": True,
            "hasRerun": rerun,
            "error": "The repair loop requires one exact Class.method --tests filter.",
        }
    return {
        "isTestCommand": True,
        "hasRerun": rerun,
        "target": {
            "module": next(iter(modules)),
            "className": target.rsplit(".", 1)[0],
            "methodName": target.rsplit(".", 1)[1],
            "fullName": target,
        },
    }


def _tool_command(root: dict[str, Any], adapter: str) -> str:
    tool_input = root.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        raise RuntimeError(f"{adapter} hook payload has no shell command at its documented path")
    return command


def _payload_text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _claude_output(root: dict[str, Any], event_name: str) -> tuple[str, int | None]:
    if event_name == "PostToolUseFailure":
        return _payload_text(root.get("error")), None
    return _payload_text(root.get("tool_response")), None


def _codex_output(root: dict[str, Any], _event_name: str) -> tuple[str, int | None]:
    return _payload_text(root.get("tool_response")), None


def parse_hook_event(text: str, before: bool, adapter: str) -> dict[str, Any]:
    try:
        root = json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Hook input is not valid JSON: {error}") from error
    if not isinstance(root, dict):
        raise RuntimeError("Hook input must be a JSON object.")
    event_name = root.get("hook_event_name")
    if not isinstance(event_name, str) or not event_name:
        raise RuntimeError("Hook payload has no top-level hook_event_name")
    dialect = resolve_adapter(adapter)
    command = _tool_command(root, dialect)
    extractor = {
        "claude": _claude_output,
        "codex": _codex_output,
    }[dialect]
    output, exit_code = extractor(root, event_name)
    return {
        "eventName": event_name,
        "adapter": dialect,
        "command": command,
        "output": output,
        "exitCode": exit_code,
    }


def _active_item(queue: dict[str, Any]) -> dict[str, Any] | None:
    active = [
        item
        for item in queue.get("items", [])
        if isinstance(item, dict) and item.get("state") in ACTIVE_STATES
    ]
    if len(active) > 1:
        raise RuntimeError("Repair state is invalid: more than one active queue item exists")
    return active[0] if active else None


def _target_matches(item: dict[str, Any], target: dict[str, str]) -> bool:
    return item.get("module") == target["module"] and item.get("fullName") == target["fullName"]


def _label(item: dict[str, Any]) -> str:
    suffix = f" (Allure ID {item['allureId']})" if item.get("allureId") else ""
    return f"{item.get('fullName', '<unknown>')}{suffix}"


def _junit_snapshot(module: str) -> dict[str, str]:
    directory = REPO_ROOT / module / "build" / "test-results" / "test"
    if not directory.is_dir():
        return {}
    snapshot: dict[str, str] = {}
    for path in directory.rglob("*.xml"):
        stat = path.stat()
        relative_path = path.relative_to(directory).as_posix()
        snapshot[relative_path] = f"{stat.st_mtime_ns}:{stat.st_size}"
    return snapshot


def _read_junit_cases(
    module: str,
    previous_snapshot: dict[str, str],
) -> tuple[list[ET.Element], str]:
    directory = REPO_ROOT / module / "build" / "test-results" / "test"
    if not directory.is_dir():
        return [], "no JUnit result directory exists"
    files: list[Path] = []
    for path in directory.rglob("*.xml"):
        stat = path.stat()
        relative_path = path.relative_to(directory).as_posix()
        signature = f"{stat.st_mtime_ns}:{stat.st_size}"
        if previous_snapshot.get(relative_path) != signature:
            files.append(path)
    if not files:
        return [], "no fresh JUnit XML was produced"
    cases: list[ET.Element] = []
    try:
        for path in files:
            cases.extend(ET.parse(path).getroot().iter("testcase"))
    except (ET.ParseError, OSError) as error:
        return [], f"fresh JUnit XML is unreadable: {error}"
    return cases, ""


def inspect_junit(item: dict[str, Any]) -> tuple[str, str]:
    previous_snapshot = item.get("junitSnapshot")
    if not isinstance(previous_snapshot, dict):
        return "inconclusive", "no matching before-run JUnit snapshot exists"
    cases, error = _read_junit_cases(str(item["module"]), previous_snapshot)
    if error:
        return "inconclusive", error
    expected_names = {
        str(item.get("displayName", "")),
        str(item.get("methodName", "")),
        f"{item.get('methodName', '')}()",
    }
    matches = [
        case
        for case in cases
        if case.get("classname") == item.get("className")
        and case.get("name") in expected_names
    ]
    if not matches:
        return "inconclusive", "no fresh targeted JUnit case was produced"
    if len(matches) != 1:
        return "inconclusive", f"fresh JUnit XML contains {len(matches)} targeted cases"
    case = matches[0]
    if case.find("failure") is not None or case.find("error") is not None:
        return "failed", "the targeted JUnit case failed"
    if case.find("skipped") is not None:
        return "inconclusive", "the targeted JUnit case was skipped"
    return "passed", "the targeted JUnit case passed"


def _evidence_for(module: str) -> str:
    if module == "appium-tests":
        return "the Appium failure digest, then its screenshot, logcat, page source, and JUnit XML"
    return "the API JUnit XML and Allure request/response attachments"


def resolve_adapter(adapter: str | None) -> str:
    """Require the client configuration to name its response dialect."""
    if adapter not in ADAPTERS:
        raise RuntimeError("Hook adapter must be explicitly configured")
    return str(adapter)


def encode_before(
    _adapter: str,
    _event_name: str,
    reason: str | None,
    notice: str | None = None,
) -> dict[str, Any]:
    message = " ".join(value for value in (notice, reason) if value)
    if reason is None:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message or reason,
        }
    }


def encode_after(_adapter: str, event_name: str, context: str | None) -> dict[str, Any]:
    if not context:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }


def _record_receipt(
    phase: str,
    event: dict[str, Any],
    item: dict[str, Any] | None,
    decision: str,
    reason: str,
    *,
    state_before: str | None = None,
    proof: str | None = None,
) -> None:
    RECEIPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": now_iso(),
        "phase": phase,
        "eventName": event.get("eventName"),
        "command": event.get("command"),
        "decision": decision,
        "reason": reason,
        "itemId": item.get("id") if item else None,
        "module": item.get("module") if item else None,
        "className": item.get("className") if item else None,
        "methodName": item.get("methodName") if item else None,
        "target": item.get("fullName") if item else None,
        "allureId": item.get("allureId") if item else None,
        "stateBefore": state_before,
        "stateAfter": item.get("state") if item else None,
        "attempts": item.get("attempts") if item else None,
        "inconclusiveRuns": item.get("inconclusiveRuns") if item else None,
        "runStartedAt": item.get("runStartedAt") if item else None,
        "exitCode": event.get("exitCode"),
        "proof": proof,
    }
    with RECEIPTS_PATH.open("a", encoding="utf-8") as receipts:
        receipts.write(json.dumps(record, ensure_ascii=False) + "\n")


def process_before(event: dict[str, Any]) -> tuple[str | None, str | None]:
    parsed = parse_gradle_command(event["command"])
    if not parsed.get("isTestCommand"):
        return None, None
    with state_file_lock():
        queue = load_queue()
        if _unlock_stale_items(queue.get("items", [])):
            save_queue(queue)
        item = _active_item(queue)
        state_before = str(item.get("state")) if item else None

        def finish(
            reason: str | None,
            notice: str | None = None,
            *,
            allowed_reason: str = "allowed test command",
        ) -> tuple[str | None, str | None]:
            _record_receipt(
                "before",
                event,
                item,
                "deny" if reason else "allow",
                reason or allowed_reason,
                state_before=state_before,
            )
            return reason, notice

        pending = [
            candidate for candidate in queue.get("items", []) if candidate.get("state") == "pending"
        ]
        if item is None:
            if pending:
                return finish(
                    "The failed-test queue has pending items. Lock the next item "
                    "before running tests.",
                )
            return finish(None, allowed_reason="allowed final verification command")
        if item.get("state") == "verified":
            return finish(
                f"{_label(item)} is verified; complete or unlock its queue item "
                "before another test run.",
            )
        if item.get("state") == "exhausted":
            exhausted_budget = (
                "three inconclusive runs"
                if item.get("exhaustedReason") == "inconclusive_runs"
                else "three repair attempts"
            )
            return finish(
                f"{_label(item)} exhausted its {exhausted_budget}; complete it "
                "as blocked or skipped.",
            )
        if parsed.get("error"):
            return finish(str(parsed["error"]))
        target = parsed.get("target")
        if not isinstance(target, dict):
            return finish("The repair lock requires one exact --tests Class.method filter.")
        if not _target_matches(item, target):
            return finish(f"Repair is locked to {_label(item)}; a different test is blocked.")
        if not parsed.get("hasRerun"):
            return finish("Run the exact test fresh with --rerun or --rerun-tasks.")
        if item.get("state") == "running":
            return finish(
                f"{_label(item)} already has a test command in progress; wait for "
                "its POST hook before starting another run.",
            )
        item["state"] = "running"
        item["runStartedAt"] = now_iso()
        item["junitSnapshot"] = _junit_snapshot(str(item["module"]))
        item.setdefault("inconclusiveRuns", 0)
        save_queue(queue)
        return finish(None, allowed_reason="allowed exact fresh test")


def process_after(event: dict[str, Any]) -> str | None:
    parsed = parse_gradle_command(event["command"])
    target = parsed.get("target")
    if not parsed.get("isTestCommand") or not isinstance(target, dict):
        return None
    with state_file_lock():
        queue = load_queue()
        item = _active_item(queue)
        if (
            item is None
            or item.get("state") != "running"
            or not _target_matches(item, target)
        ):
            return None
        state_before = str(item.get("state"))
        proof, reason = inspect_junit(item)
        failed_event = event["eventName"].lower().endswith("failure") or (
            event.get("exitCode") is not None and event["exitCode"] != 0
        )
        output = str(event.get("output", "")).lower()
        if proof == "passed":
            item["state"] = "verified"
            item.pop("exhaustedReason", None)
            item.pop("pendingNotice", None)
            context = (
                f"{_label(item)} has fresh green JUnit proof and is ready for fixed completion."
            )
        elif proof == "failed":
            attempts = int(item.get("attempts", 0)) + 1
            item["attempts"] = attempts
            item["state"] = "exhausted" if attempts >= MAX_REPAIR_ATTEMPTS else "active"
            if item["state"] == "exhausted":
                item["exhaustedReason"] = "repair_attempts"
            else:
                item.pop("exhaustedReason", None)
            instruction = (
                "The attempt limit is exhausted. Complete the item as blocked or "
                "skipped; do not change app/ or fake-api/."
                if item["state"] == "exhausted"
                else (
                    f"Read {_evidence_for(str(item['module']))}, make one "
                    "evidence-backed test-layer change, and rerun this test."
                )
            )
            context = (
                f"Repair attempt {attempts}/{MAX_REPAIR_ATTEMPTS} failed for "
                f"{_label(item)}. {instruction}"
            )
        elif failed_event and any(marker in output for marker in INFRASTRUCTURE_MARKERS):
            item["state"] = "active"
            context = (
                f"Infrastructure failure detected for {_label(item)} without a fresh "
                "failed target JUnit case. No repair attempt was consumed. Restore "
                "fake-api/Appium/device connectivity and rerun the same exact test."
            )
        else:
            inconclusive_runs = int(item.get("inconclusiveRuns", 0)) + 1
            item["inconclusiveRuns"] = inconclusive_runs
            item["state"] = (
                "exhausted" if inconclusive_runs >= MAX_INCONCLUSIVE_RUNS else "active"
            )
            if item["state"] == "exhausted":
                item["exhaustedReason"] = "inconclusive_runs"
                instruction = (
                    "The inconclusive-run limit is exhausted. Complete the item as "
                    "blocked or skipped after recording the compilation or reporting problem."
                )
            else:
                item.pop("exhaustedReason", None)
                instruction = (
                    "Restore compilation and test reporting, then rerun the same exact test."
                )
            context = (
                f"Inconclusive run {inconclusive_runs}/{MAX_INCONCLUSIVE_RUNS} for "
                f"{_label(item)} consumed no repair attempt: {reason}. "
                "Compile-only, cached, skipped, and zero-test runs are not green proof. "
                f"{instruction}"
            )
        item.pop("junitSnapshot", None)
        item.pop("pendingNotice", None)
        save_queue(queue)
        _record_receipt(
            "after",
            event,
            item,
            "observe",
            context,
            state_before=state_before,
            proof=proof,
        )
    return context


def hook_main(phase: str, adapter: str) -> int:
    text = sys.stdin.read()
    before = phase == "before"
    dialect = resolve_adapter(adapter)
    event_name = "PreToolUse" if before else "PostToolUse"
    try:
        event = parse_hook_event(text, before, adapter)
        event_name = event["eventName"]
        dialect = str(event["adapter"])
        if before:
            reason, notice = process_before(event)
            result = encode_before(dialect, event_name, reason, notice)
        else:
            result = encode_after(dialect, event_name, process_after(event))
    except Exception as error:  # Hooks must return a valid tool-specific envelope.
        message = f"test-repair hook failed{' closed' if before else ''}: {error}"
        result = (
            encode_before(dialect, event_name, message)
            if before
            else encode_after(dialect, event_name, message)
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=resolve_project_root,
        default=str(REPO_ROOT),
        help=(
            "Kotlin project containing appium-tests or api-tests "
            "(default: repository containing this script)"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for phase in ("before", "after"):
        hook_parser = commands.add_parser(phase)
        hook_parser.add_argument(
            "--adapter",
            choices=ADAPTERS,
            required=True,
        )
    refresh_parser = commands.add_parser("refresh")
    refresh_parser.add_argument("--if-changed", action="store_true")
    lock_parser = commands.add_parser("lock")
    lock_parser.add_argument("--worker")
    unlock_parser = commands.add_parser("unlock")
    unlock_parser.add_argument("--id", required=True)
    complete_parser = commands.add_parser("complete")
    complete_parser.add_argument("--id", required=True)
    complete_parser.add_argument(
        "--outcome", choices=("fixed", "blocked", "skipped"), required=True
    )
    complete_parser.add_argument("--reason")
    commands.add_parser("show")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        configure_project_root(arguments.project_root)
        if arguments.command in {"before", "after"}:
            return hook_main(arguments.command, arguments.adapter)
        if arguments.command == "refresh":
            return refresh(arguments.if_changed)
        if arguments.command == "lock":
            return lock(arguments.worker)
        if arguments.command == "unlock":
            return unlock(arguments.id)
        if arguments.command == "complete":
            return complete(arguments.id, arguments.outcome, arguments.reason)
        return show()
    except RuntimeError as error:
        print(f"test-repair: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
