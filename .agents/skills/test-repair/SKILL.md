---
name: test-repair
description: >-
  Investigate and repair one failing Kotlin API or Appium test, either from an
  explicit Class.method target or from the repository-local Allure-backed
  repair queue. Use when invoked as $test-repair or /test-repair, or when asked
  to fix, diagnose, or process the next failed test. Runs the exact test,
  classifies the cause, changes a test layer only for an evidence-backed test
  automation bug, and stops on product bugs or other documented conditions.
---

# test-repair

Repair one exact test method at a time. The canonical queue, state, command
guard, and fresh-proof implementation is `.agents/hooks/test_repair.py`. It
uses only the Python 3.11+ standard library. Generated state is ignored by
Git.

## 1. Choose and lock the target

When no target was supplied, refresh the Allure-backed state and lock the
next failure:

```powershell
python .agents/hooks/test_repair.py refresh
python .agents/hooks/test_repair.py lock
```

On macOS/Linux, replace `python` with `python3`. `refresh` prefers raw
`build/allure-results/*-result.json` and falls back to generated Allure report
data. `lock` is atomic, prints one item, and does not consume either run
budget. One repair lock may be held across the whole repository, so the next
failure cannot be started until the current one is completed or unlocked. Stop
cleanly when the queue is empty.

For an explicit target, resolve its module and exact `Class.method` before
execution. If pending queue work exists, lock that work first; the command
guard does not allow bypassing queued failures.

A lock older than two hours returns to pending. Never repair an item locked by
another worker. Unlock unfinished work, including a run that was interrupted
before its POST hook recorded a result, with:

```powershell
python .agents/hooks/test_repair.py unlock --id <id>
```

## 2. Check the test environment

- API tests need `fake-api` running on port 8080.
- Appium tests need a booted emulator, Appium on `127.0.0.1:4723`, the correct
  APK, disabled animations, and a reset sandbox state. Follow the
  `run-appium-suite` skill for platform-specific setup.
- Run one exact method with one direct Gradle-wrapper invocation and
  `--tests Class.method --rerun` (or `--rerun-tasks`). Never use a class-wide
  filter, aggregate `test`, `check`, or `build` tasks, shell operators,
  redirections, or batch repair candidates.
- If compilation or test reporting must be restored before a rerun, use a
  non-test task such as `:api-tests:compileTestKotlin`,
  `:appium-tests:compileTestKotlin`, or `assemble`. Do not use `build` or
  `check` while the repair queue is active.

## 3. Read evidence before editing

Use the smallest decisive evidence first:

1. Appium run digest, then screenshot, logcat, page source, and JUnit XML.
2. API JUnit XML, then the matching Allure request/response attachments.
3. Raw Gradle output only when compilation or test reporting failed before
   producing structured evidence.

Record one triage outcome before changing code:

- `PRODUCT_BUG`: the application or backend under test is wrong;
- `TEST_AUTOMATION_BUG`: the test, its data, setup or configuration is wrong;
- `INFRASTRUCTURE_ISSUE`: the SDK, runner, emulator, device, Appium or network
  is unavailable or misconfigured;
- `NEEDS_INVESTIGATION`: the available evidence does not distinguish the
  causes yet.

Record reproduction separately as `DETERMINISTIC`, `FLAKY`, or
`NOT_VERIFIED`. `FLAKY` describes changing outcomes for the same code and
conditions; it does not identify the cause or authorize a blind retry. An exit
code, `BUILD SUCCESSFUL`, cached output, or compilation success is not green
proof.

## 4. Apply one allowed fix

Make one evidence-backed change, then rerun the same exact method. Allowed
test layers are `tests/`, `actions/`, `pages/`, `testdata/`, and `client/`.

- Never weaken assertions or edit unrelated logic.
- Never edit `app/`, `fake-api/`, fixtures, graders,
  `rule/DriverFactory.kt`, or another protected path to green a test.
- Stop and report a product bug when the evidence points to the application
  or backend under test.
- Stop and name the missing evidence when the outcome is
  `NEEDS_INVESTIGATION`.
- Restore the environment before rerunning an `INFRASTRUCTURE_ISSUE`.
- Change a test layer only for an evidence-backed `TEST_AUTOMATION_BUG`.
- Use testTag locators, not xpath-by-text. Keep mobile asserts in actions and
  pages assert-free. Do not add `Thread.sleep`.
- Preserve load-bearing test data and UI copy instead of updating an
  expectation to whatever a broken run produced.

## 5. Respect both budgets

- Fresh JUnit XML containing the one matching failed or errored case consumes
  one of three repair attempts.
- Compile-only, cached, skipped, zero-test, stale, or otherwise inconclusive
  runs consume no repair attempt, but consume one of three inconclusive-run
  slots.
- A recognized infrastructure failure consumes neither budget only when no
  fresh failed target JUnit case exists. Stop and restore fake-api, Appium, or
  emulator/device connectivity before rerunning.
- Fresh JUnit XML containing exactly one matching passing, non-skipped case is
  the only green proof.

After either three counted failures or three inconclusive runs, the item is
`exhausted`. Do not run it again; complete it as blocked or skipped with the
reason.

## 6. Complete and verify

Use `fixed` only after the hook marks the item `verified` from fresh XML:

```powershell
python .agents/hooks/test_repair.py complete --id <id> --outcome fixed
python .agents/hooks/test_repair.py complete --id <id> --outcome blocked --reason "product bug: ..."
python .agents/hooks/test_repair.py complete --id <id> --outcome skipped --reason "stop condition: ..."
```

The complete queue and active repair state live in
`.agent-state/test_repair.json`. PRE/POST decisions are appended to
`.agent-state/test_repair_receipts.jsonl`.
`test_repair.lock` is an internal, temporary file lock; use the `unlock`
command to release a test from the repair workflow, and never delete this file
manually.

After the queue is empty, run the relevant full suite when requested. Format
changed Kotlin with ktlint before final verification.

## Report

State the exact target and Allure ID when present, decisive evidence, triage
outcome, reproduction status, one change per counted attempt, inconclusive and
repair counts, targeted result, requested suite result, and final queue
outcome. Do not commit unless the user explicitly asks.
