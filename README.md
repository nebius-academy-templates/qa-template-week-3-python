# Week 3 Python: test repair workflow

Use `test_repair.py` to select one failed Kotlin test from Allure results and
keep its triage state explicit. The script uses only the Python standard
library. It doesn't require third-party packages. A virtual environment is
optional; use one, or use `uv` if you extend the implementation with your own
dependencies.

## Install Python

Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/)
or the operating system package manager.

### Windows

1. Run the Windows installer.
2. Enable "Add python.exe to PATH" in the installer.
3. Open a new PowerShell window.
4. Verify the installation:

```powershell
python --version
```

### macOS or Linux

1. Install Python 3.11 or newer.
2. Open a new terminal.
3. Verify the installation:

```bash
python3 --version
```

Check that the reported version is 3.11 or newer.

## Add the script and skill to the Kotlin repository

You already have the Kotlin practice repository from the previous course
modules. From the directory that contains it, clone this repository and copy
the workflow plus the prepared API failure.

### Windows PowerShell

```powershell
git clone https://github.com/nebius-academy-templates/qa-template-week-3-python.git
Set-Location AI-for-Kotlin-practice
New-Item -ItemType Directory -Force ".agents\hooks" | Out-Null
New-Item -ItemType Directory -Force ".agents\skills" | Out-Null
Copy-Item "..\qa-template-week-3-python\test_repair.py" ".agents\hooks\test_repair.py"
Copy-Item -Recurse -Force "..\qa-template-week-3-python\.agents\skills\test-repair" ".agents\skills\test-repair"
Copy-Item "..\qa-template-week-3-python\api-tests\PreparedApiFailureTest.kt" "api-tests\src\test\kotlin\tests\PreparedApiFailureTest.kt"
```

### macOS or Linux

```bash
git clone https://github.com/nebius-academy-templates/qa-template-week-3-python.git
cd AI-for-Kotlin-practice
mkdir -p .agents/hooks .agents/skills
cp ../qa-template-week-3-python/test_repair.py .agents/hooks/test_repair.py
cp -R ../qa-template-week-3-python/.agents/skills/test-repair .agents/skills/test-repair
cp ../qa-template-week-3-python/api-tests/PreparedApiFailureTest.kt api-tests/src/test/kotlin/tests/PreparedApiFailureTest.kt
```

Install the skill in the directory used by your coding agent:

- **Codex:** Copy the complete `.agents/skills/test-repair/` directory to
  `AI-for-Kotlin-practice/.agents/skills/test-repair/`.
- **Claude Code:** Copy `.agents/skills/test-repair/SKILL.md` to
  `AI-for-Kotlin-practice/.claude/skills/test-repair/SKILL.md`.
- If you use both coding agents, install both copies.

Create any missing directories before copying the skill, then restart the
coding-agent session. You don't need to edit `MIRRORED_SKILLS` or run the skill
sync script.

## Apply the Appium failure-digest hotfix

Before the mobile triage exercise, apply the compatibility patch to the
existing Kotlin practice checkout. Don't clone the Kotlin repository again.
Follow [`hotfixes/README.md`](hotfixes/README.md) and verify the dedicated
formatter tests before processing a mobile failure.

The copied files have this layout:

```text
AI-for-Kotlin-practice/
├── .agents/
    ├── hooks/
    │   └── test_repair.py
    └── skills/
        └── test-repair/
            ├── SKILL.md
            └── agents/openai.yaml
└── api-tests/src/test/kotlin/tests/
    └── PreparedApiFailureTest.kt
```

`PreparedApiFailureTest` contains one intentional assertion defect and
produces a small JUnit failure plus Allure request and response attachments for
the triage exercise. Remove the copied test after the exercise; don't commit
it to the Kotlin repository.

## Enable the repair guard

Copy the ready hook configuration for each coding agent used in the course:

```text
hook-configs/.codex/hooks.json     → AI-for-Kotlin-practice/.codex/hooks.json
hook-configs/.claude/settings.json → AI-for-Kotlin-practice/.claude/settings.json
```

The Claude Code configuration checks that `python3` can run Python 3.11 or newer,
then tries `python` if that check fails. This skips a Microsoft Store alias that
exists on PATH but can't run Python. If neither interpreter works, or the hook
process exits with an error, PRE blocks the command with exit code `2`; POST
reports the error. The interpreter check leaves the hook event on stdin intact.

The Codex configuration uses `python` on Windows and `python3` on macOS or Linux.
No per-platform edit is required.

The Claude Code configuration runs its command through bash. On Windows, that is
the Git Bash shipped with Git for Windows, which the course already requires.

The Week 1 repository contains empty hook configurations, so adding the script
alone doesn't enable the guard. Install the Codex or Claude Code configuration
before the hook practice. If a target file already contains custom hooks, merge
the supplied hook entries instead of replacing them. Restart the coding-agent
session after the configuration changes.

The hooks check each shell command for a test invocation. Their status messages
describe that check; they don't mean a test ran or results were recorded.
Commands unrelated to test execution leave the repair queue unchanged.

Run the following commands from the root of `AI-for-Kotlin-practice`.

## Inspect the CLI

### Windows PowerShell

```powershell
python ".agents\hooks\test_repair.py" --help
```

### macOS or Linux

```bash
python3 .agents/hooks/test_repair.py --help
```

The setup is complete when `--help` lists `refresh`, `show`, `lock`, `unlock`,
and `complete`.

## Process one failure

### Windows PowerShell

```powershell
python ".agents\hooks\test_repair.py" refresh
python ".agents\hooks\test_repair.py" show
python ".agents\hooks\test_repair.py" lock
```

### macOS or Linux

```bash
python3 .agents/hooks/test_repair.py refresh
python3 .agents/hooks/test_repair.py show
python3 .agents/hooks/test_repair.py lock
```

## Inspect hook decisions

Show the queue and the last three hook receipts as a compact table:

### Windows PowerShell

```powershell
python .agents/hooks/test_repair.py show --receipts 3
```

### macOS or Linux

```bash
python3 .agents/hooks/test_repair.py show --receipts 3
```

The table shows `decision`, `transition`, `attempts`, and `proof` from
`.agent-state/test_repair_receipts.jsonl`, in recorded order. Replace `3` with
another positive count. The receipts are the latest entries for the project,
not filtered by queue item. `show` is read-only: it doesn't recover expired
locks, change counters, or run tests. Without `--receipts`, its output is unchanged.

## Record the triage decision

The script selects and locks the exact failed test. Only one repair lock may be
active in the repository; unlock or complete it before locking another. The
script doesn't diagnose the failure. Read the matching failure digest and
record one triage outcome:

- `PRODUCT_BUG`: The application or backend under test is wrong.
- `TEST_AUTOMATION_BUG`: The test, its data, setup, or configuration is wrong.
- `INFRASTRUCTURE_ISSUE`: The SDK, runner, emulator, device, Appium, or network
  is unavailable or misconfigured.
- `NEEDS_INVESTIGATION`: The available evidence doesn't distinguish the
  causes yet.

Record reproduction separately:

- `DETERMINISTIC`: The same conditions produce the same result.
- `FLAKY`: The result changes with the same code and conditions.
- `NOT_VERIFIED`: Reproduction hasn't been checked yet.

`FLAKY` is not a cause or a fifth triage outcome. A flaky result may come from
the product, test automation, or infrastructure.

Use the `id` returned by `lock` when work is interrupted or must return to the
queue:

```powershell
python ".agents\hooks\test_repair.py" unlock --id <id>
```

On macOS or Linux, run the same command with `python3` and forward slashes.

The script reads test evidence from the Kotlin project and writes generated
queue state to `.agent-state/`. It doesn't diagnose a failure or edit Kotlin
code by itself; the coding agent performs that work through the `test-repair`
skill, including the exact Gradle run and any evidence-backed test-layer fix.
Commit the hook and canonical skill to the Kotlin repository after the lesson.
Don't commit `.agent-state/`, the prepared failure test, or generated test
evidence.
