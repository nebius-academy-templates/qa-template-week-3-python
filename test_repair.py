#!/usr/bin/env python3
"""Shared exact-test repair queue and shell guard for Codex, Claude, and Cursor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
STATE_LOCK_PATH = STATE_DIR / "test_repair.lock"
RECEIPTS_PATH = STATE_DIR / "test_repair_receipts.jsonl"

FAILED_STATUSES = {"failed", "broken"}
TERMINAL_STATES = {"done", "blocked", "skipped"}
CLAIM_TTL = timedelta(hours=2)
MAX_REPAIR_ATTEMPTS = 3
MAX_INCONCLUSIVE_RUNS = 3

# The dialect is supplied by the client configuration, never guessed from the
# payload: a malformed event must still be answered in the caller's format.
ADAPTERS = ("codex", "claude", "cursor")
ADAPTER_AUTO = "auto"
CURSOR_EVENT_NAMES = {
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
}
# Cursor exposes no response fields on its failure events.
CURSOR_SILENT_EVENTS = {"postToolUseFailure", "afterShellExecution", "afterMCPExecution"}

EXIT_CODE_RE = re.compile(r"^Exit code (\d+)\b", re.IGNORECASE | re.MULTILINE)
TEST_TASKS = {
    ":api-tests:test": "api-tests",
    "api-tests:test": "api-tests",
    ":appium-tests:test": "appium-tests",
    "appium-tests:test": "appium-tests",
}
INFRASTRUCTURE_MARKERS = (
    "sessionnotcreatedexception",
    "uiautomation not connected",
    "uiautomator2 server",
    "device offline",
    "no devices/emulators found",
    "connection refused",
    "connectexception",
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
    global REPO_ROOT, STATE_DIR, STATE_PATH, STATE_LOCK_PATH, RECEIPTS_PATH
    REPO_ROOT = root
    STATE_DIR = root / ".agent-state"
    STATE_PATH = STATE_DIR / "test_repair.json"
    STATE_LOCK_PATH = STATE_DIR / "test_repair.lock"
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
def state_lock(timeout_seconds: float = 10.0) -> Iterator[None]:
    """Serialize repair-state mutations without third-party dependencies."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            descriptor = os.open(
                STATE_LOCK_PATH,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            try:
                os.write(descriptor, f"{os.getpid()}\n".encode())
            except OSError:
                STATE_LOCK_PATH.unlink(missing_ok=True)
                raise
            finally:
                os.close(descriptor)
            break
        except FileExistsError:
            try:
                stale = time.time() - STATE_LOCK_PATH.stat().st_mtime > 60
            except FileNotFoundError:
                continue
            if stale:
                STATE_LOCK_PATH.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Test-repair state is locked: {STATE_LOCK_PATH}"
                ) from None
            time.sleep(0.05)
    try:
        yield
    finally:
        STATE_LOCK_PATH.unlink(missing_ok=True)


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
        stat = path.stat()
        digest.update(f"{module}:{path}:{stat.st_mtime_ns}:{stat.st_size}\n".encode())
    return digest.hexdigest()


def _label_value(result: dict[str, Any], name: str) -> str | None:
    for label in result.get("labels", []):
        if isinstance(label, dict) and label.get("name") == name:
            value = label.get("value")
            return value if isinstance(value, str) else None
    return None


def normalize_result(module: str, path: Path) -> dict[str, Any] | None:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict):
        return None
    status = str(result.get("status", "")).lower()
    full_name = result.get("fullName") or result.get("testCaseName")
    if not isinstance(full_name, str) or "." not in full_name:
        return None
    class_name = _label_value(result, "testClass") or _label_value(result, "suite")
    method_name = _label_value(result, "testMethod")
    class_name = class_name or full_name.rsplit(".", 1)[0]
    method_name = method_name or full_name.rsplit(".", 1)[1]
    stopped_at = int(result.get("stop") or path.stat().st_mtime_ns // 1_000_000)
    details = result.get("statusDetails") or result.get("error") or {}
    details = details if isinstance(details, dict) else {}
    message_lines = str(details.get("message") or "").splitlines()
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
    with state_lock():
        queue = load_queue()
        if if_changed and queue.get("sourceFingerprint") == fingerprint:
            return 0
        newest: dict[str, dict[str, Any]] = {}
        for module, path in files:
            result = normalize_result(module, path)
            if result is None:
                continue
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
                    "claimedBy",
                    "claimedAt",
                    "runStartedAt",
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


def _release_stale_claims(items: list[dict[str, Any]]) -> bool:
    cutoff = now() - CLAIM_TTL
    changed = False
    for item in items:
        claimed_at = parse_iso(item.get("claimedAt"))
        if (
            item.get("state") in {"claimed", "active", "running"}
            and claimed_at
            and claimed_at < cutoff
        ):
            item["state"] = "pending"
            for field in ("claimedBy", "claimedAt", "runStartedAt", "pendingNotice"):
                item.pop(field, None)
            changed = True
    return changed


def claim(worker: str | None = None) -> int:
    with state_lock():
        queue = load_queue()
        items = queue.get("items", [])
        _release_stale_claims(items)
        pending = next(
            (item for item in items if isinstance(item, dict) and item.get("state") == "pending"),
            None,
        )
        if pending is None:
            save_queue(queue)
            print("Queue empty: no pending failed tests")
            return 0
        pending["state"] = "claimed"
        pending["claimedBy"] = worker or f"{socket.gethostname()}:{os.getpid()}"
        pending["claimedAt"] = now_iso()
        pending.setdefault("attempts", 0)
        pending.setdefault("inconclusiveRuns", 0)
        save_queue(queue)
        print(json.dumps(pending, ensure_ascii=False, indent=2))
    return 0


def complete(item_id: str, outcome: str, reason: str | None = None) -> int:
    with state_lock():
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
        if outcome != "fixed" and state not in {"claimed", "active", "exhausted", "verified"}:
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


def release(item_id: str) -> int:
    with state_lock():
        queue = load_queue()
        item = next(
            (candidate for candidate in queue.get("items", []) if candidate.get("id") == item_id),
            None,
        )
        if item is None:
            print(f"Unknown queue id: {item_id}", file=sys.stderr)
            return 1
        if item.get("state") not in {"claimed", "active"}:
            print(f"Queue item cannot be released from state {item.get('state')}", file=sys.stderr)
            return 1
        item["state"] = "pending"
        for field in ("claimedBy", "claimedAt", "runStartedAt", "pendingNotice"):
            item.pop(field, None)
        save_queue(queue)
        print(f"{item_id}: released")
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


def parse_gradle_command(command: str) -> dict[str, Any]:
    tokens, parse_error = _command_tokens(command)
    if parse_error:
        rough_tokens = {
            token.strip("\"'();&|")
            for token in command.split()
        }
        is_test_command = bool(rough_tokens.intersection(TEST_TASKS))
        return (
            {"isTestCommand": True, "error": parse_error}
            if is_test_command
            else {"isTestCommand": False}
        )
    modules = {TEST_TASKS[token] for token in tokens if token in TEST_TASKS}
    if not modules:
        return {"isTestCommand": False}
    if len(modules) != 1:
        return {"isTestCommand": True, "error": "Run one test module at a time."}
    filters: list[str] = []
    rerun = any(token in {"--rerun", "--rerun-tasks"} for token in tokens)
    for index, token in enumerate(tokens):
        if token == "--tests":
            if index + 1 >= len(tokens):
                return {
                    "isTestCommand": True,
                    "hasRerun": rerun,
                    "error": "The --tests option requires a value.",
                }
            filters.append(_unquote(tokens[index + 1]))
        elif token.startswith("--tests="):
            filters.append(_unquote(token.removeprefix("--tests=")))
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


def _nested_values(value: Any, keys: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys:
                found.append(child)
            found.extend(_nested_values(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.extend(_nested_values(child, keys))
    return found


def parse_hook_event(text: str, before: bool, adapter: str = ADAPTER_AUTO) -> dict[str, Any]:
    try:
        root = json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Hook input is not valid JSON: {error}") from error
    if not isinstance(root, dict):
        raise RuntimeError("Hook input must be a JSON object.")
    event_values = _nested_values(root, {"hook_event_name", "hookEventName"})
    event_name = next((value for value in event_values if isinstance(value, str)), None)
    event_name = event_name or ("PreToolUse" if before else "PostToolUse")
    commands = _nested_values(root, {"command"})
    command = next((value for value in commands if isinstance(value, str)), "")
    output_values = _nested_values(
        root,
        {
            "output",
            "stdout",
            "stderr",
            "error",
            "error_message",
            "tool_output",
            "toolOutput",
            "tool_response",
            "toolResponse",
            "tool_result",
        },
    )
    output_parts = [
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for value in output_values
        if value is not None
    ]
    output = "\n".join(dict.fromkeys(output_parts))
    exit_values = _nested_values(root, {"exit_code", "exitCode"})
    exit_code = next((value for value in exit_values if isinstance(value, int)), None)
    if exit_code is None:
        for value in output_parts:
            try:
                embedded = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            candidates = _nested_values(embedded, {"exit_code", "exitCode"})
            exit_code = next((item for item in candidates if isinstance(item, int)), None)
            if exit_code is not None:
                break
    if exit_code is None:
        # Codex and Cursor may serialize the code only in tool output. Claude
        # can omit it entirely; its PostToolUseFailure event is authoritative.
        match = EXIT_CODE_RE.search(output)
        exit_code = int(match.group(1)) if match else None
    return {
        "eventName": event_name,
        "adapter": resolve_adapter(adapter, event_name),
        "command": command,
        "output": output,
        "exitCode": exit_code,
    }


def _active_item(queue: dict[str, Any]) -> dict[str, Any] | None:
    active = {"claimed", "active", "running", "verified", "exhausted"}
    return next(
        (
            item
            for item in queue.get("items", [])
            if isinstance(item, dict) and item.get("state") in active
        ),
        None,
    )


def _target_matches(item: dict[str, Any], target: dict[str, str]) -> bool:
    return item.get("module") == target["module"] and item.get("fullName") == target["fullName"]


def _label(item: dict[str, Any]) -> str:
    suffix = f" (Allure ID {item['allureId']})" if item.get("allureId") else ""
    return f"{item.get('fullName', '<unknown>')}{suffix}"


def _read_junit_cases(module: str, started_at: datetime) -> tuple[list[ET.Element], str]:
    directory = REPO_ROOT / module / "build" / "test-results" / "test"
    if not directory.is_dir():
        return [], "no JUnit result directory exists"
    cutoff = started_at.timestamp() - 1.0
    files = [path for path in directory.rglob("*.xml") if path.stat().st_mtime >= cutoff]
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
    started_at = parse_iso(item.get("runStartedAt"))
    if started_at is None:
        return "inconclusive", "no matching before-run marker exists"
    cases, error = _read_junit_cases(str(item["module"]), started_at)
    if error:
        return "inconclusive", error
    if len(cases) != 1:
        return "inconclusive", f"fresh JUnit XML contains {len(cases)} cases"
    case = cases[0]
    if case.get("classname") != item.get("className"):
        return "inconclusive", "fresh JUnit XML belongs to another class"
    if case.find("failure") is not None or case.find("error") is not None:
        return "failed", "the targeted JUnit case failed"
    if case.find("skipped") is not None:
        return "inconclusive", "the targeted JUnit case was skipped"
    return "passed", "the targeted JUnit case passed"


def _evidence_for(module: str) -> str:
    if module == "appium-tests":
        return "the Appium failure digest, then its screenshot, logcat, page source, and JUnit XML"
    return "the API JUnit XML and Allure request/response attachments"


def resolve_adapter(adapter: str | None, event_name: str) -> str:
    """Trust the configured dialect; fall back to the event name only for it."""
    if adapter in ADAPTERS:
        return str(adapter)
    return "cursor" if event_name in CURSOR_EVENT_NAMES else "claude"


def _hook_channel(adapter: str, event_name: str, before: bool) -> str:
    if adapter == "cursor":
        if before:
            return "cursor_permission"
        return "none" if event_name in CURSOR_SILENT_EVENTS else "cursor_context"
    # Codex and Claude share the hookSpecificOutput envelope.
    return "claude_permission" if before else "claude_context"


def encode_before(
    adapter: str,
    event_name: str,
    reason: str | None,
    notice: str | None = None,
) -> dict[str, Any]:
    message = " ".join(value for value in (notice, reason) if value)
    if _hook_channel(adapter, event_name, True) == "cursor_permission":
        result: dict[str, Any] = {"permission": "allow" if reason is None else "deny"}
        if message:
            result.update({"user_message": message, "agent_message": message})
        return result
    if reason is None:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message or reason,
        }
    }


def encode_after(adapter: str, event_name: str, context: str | None) -> dict[str, Any]:
    if not context:
        return {}
    channel = _hook_channel(adapter, event_name, False)
    if channel == "cursor_context":
        return {"additional_context": context}
    if channel == "none":
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
    with state_lock():
        queue = load_queue()
        if _release_stale_claims(queue.get("items", [])):
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

        if (
            item
            and item.get("pendingNotice")
            and _hook_channel(
                resolve_adapter(event.get("adapter"), event["eventName"]),
                event["eventName"],
                True,
            )
            == "cursor_permission"
        ):
            notice = item.pop("pendingNotice")
            save_queue(queue)
            return finish(
                "This test command was paused to deliver the previous repair result.",
                str(notice),
            )
        pending = [
            candidate for candidate in queue.get("items", []) if candidate.get("state") == "pending"
        ]
        if item is None:
            if pending:
                return finish(
                    "The failed-test queue has pending items. Claim the next item "
                    "before running tests.",
                )
            return finish(None, allowed_reason="allowed final verification command")
        if item.get("state") == "verified":
            return finish(
                f"{_label(item)} is verified; complete or release its queue item "
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
        item["state"] = "running"
        item["runStartedAt"] = now_iso()
        item.setdefault("inconclusiveRuns", 0)
        save_queue(queue)
        return finish(None, allowed_reason="allowed exact fresh test")


def process_after(event: dict[str, Any]) -> str | None:
    parsed = parse_gradle_command(event["command"])
    target = parsed.get("target")
    if not parsed.get("isTestCommand") or not isinstance(target, dict):
        return None
    with state_lock():
        queue = load_queue()
        item = _active_item(queue)
        if item is None or not _target_matches(item, target):
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
        elif failed_event and any(marker in output for marker in INFRASTRUCTURE_MARKERS):
            item["state"] = "active"
            context = (
                f"Infrastructure failure detected for {_label(item)}. "
                "No repair attempt was consumed. Restore fake-api/Appium/device "
                "connectivity and rerun the same exact test."
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
        can_deliver = (
            _hook_channel(
                resolve_adapter(event.get("adapter"), event["eventName"]),
                event["eventName"],
                False,
            )
            != "none"
        )
        if can_deliver:
            item.pop("pendingNotice", None)
        else:
            item["pendingNotice"] = context
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


def hook_main(phase: str, adapter: str = ADAPTER_AUTO) -> int:
    text = sys.stdin.read()
    before = phase == "before"
    dialect = adapter if adapter in ADAPTERS else "claude"
    # Until the payload is parsed the concrete event is unknown. Cursor
    # registers one AFTER command for both postToolUse and postToolUseFailure,
    # and the failure event accepts no response fields, so an unparsed Cursor
    # AFTER must answer with the silent event rather than the talking one.
    if before:
        event_name = "preToolUse" if dialect == "cursor" else "PreToolUse"
    elif dialect == "cursor":
        event_name = "postToolUseFailure"
    else:
        event_name = "PostToolUse"
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


def should_skip_compatibility_hook(config_source: str) -> bool:
    """Avoid running a Claude hook twice when Cursor imports Claude settings."""
    return config_source == "claude" and bool(os.environ.get("CURSOR_VERSION"))


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
            "--config-source",
            choices=("native", "claude"),
            default="native",
        )
        hook_parser.add_argument(
            "--adapter",
            choices=(*ADAPTERS, ADAPTER_AUTO),
            default=ADAPTER_AUTO,
        )
    refresh_parser = commands.add_parser("refresh")
    refresh_parser.add_argument("--if-changed", action="store_true")
    claim_parser = commands.add_parser("claim")
    claim_parser.add_argument("--worker")
    release_parser = commands.add_parser("release")
    release_parser.add_argument("--id", required=True)
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
    configure_project_root(arguments.project_root)
    if arguments.command in {"before", "after"}:
        if should_skip_compatibility_hook(arguments.config_source):
            return 0
        return hook_main(arguments.command, arguments.adapter)
    if arguments.command == "refresh":
        return refresh(arguments.if_changed)
    if arguments.command == "claim":
        return claim(arguments.worker)
    if arguments.command == "release":
        return release(arguments.id)
    if arguments.command == "complete":
        return complete(arguments.id, arguments.outcome, arguments.reason)
    return show()


if __name__ == "__main__":
    raise SystemExit(main())
