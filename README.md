# Week 3 Python: test repair workflow

Use `test_repair.py` to select one failed Kotlin test from Allure results and
keep its triage state explicit. The script uses only the Python standard
library. It does not require third-party packages. A virtual environment is
optional; use one, or use `uv`, if you extend the implementation with your own
dependencies.

## Install Python

Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/)
or the operating system package manager.

Windows:

1. Run the Windows installer.
2. Enable **Add python.exe to PATH** in the installer.
3. Open a new PowerShell window.
4. Verify the installation:

```powershell
python --version
```

macOS or Linux:

1. Install Python 3.11 or newer.
2. Open a new terminal.
3. Verify the installation:

```bash
python3 --version
```

Stop if the reported version is older than 3.11.

## Add the script and skill to the Kotlin repository

The Kotlin practice repository already exists from the previous course
modules. From the directory that contains it, clone this repository and copy
the workflow plus the prepared API failure.

Windows PowerShell:

```powershell
git clone https://github.com/nebius-academy-templates/qa-template-week-3-python.git
Set-Location AI-for-Kotlin-practice
New-Item -ItemType Directory -Force ".agents\hooks" | Out-Null
New-Item -ItemType Directory -Force ".agents\skills" | Out-Null
Copy-Item "..\qa-template-week-3-python\test_repair.py" ".agents\hooks\test_repair.py"
Copy-Item -Recurse -Force "..\qa-template-week-3-python\.agents\skills\test-repair" ".agents\skills\test-repair"
Copy-Item "..\qa-template-week-3-python\api-tests\PreparedApiFailureTest.kt" "api-tests\src\test\kotlin\tests\PreparedApiFailureTest.kt"
```

macOS or Linux:

```bash
git clone https://github.com/nebius-academy-templates/qa-template-week-3-python.git
cd AI-for-Kotlin-practice
mkdir -p .agents/hooks .agents/skills
cp ../qa-template-week-3-python/test_repair.py .agents/hooks/test_repair.py
cp -R ../qa-template-week-3-python/.agents/skills/test-repair .agents/skills/test-repair
cp ../qa-template-week-3-python/api-tests/PreparedApiFailureTest.kt api-tests/src/test/kotlin/tests/PreparedApiFailureTest.kt
```

## Apply the Appium failure-digest hotfix

Before the mobile triage exercise, apply the compatibility patch to the
existing Kotlin practice checkout. Do not clone the Kotlin repository again.
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
the triage exercise. Remove the copied test after the exercise; do not commit
it to the Kotlin repository.

## Enable the repair guard

Copy the ready hook configuration for each coding agent used in the course:

```text
hook-configs/.codex/hooks.json     → AI-for-Kotlin-practice/.codex/hooks.json
hook-configs/.claude/settings.json → AI-for-Kotlin-practice/.claude/settings.json
```

The Week 1 repository contains empty hook configurations, so adding the script
alone does not enable the guard. Install the Codex or Claude Code configuration
before the hook practice. If a target file already contains custom hooks, merge
the supplied hook entries instead of replacing them. Restart the coding-agent
session after the configuration changes.

Run the following commands from the root of `AI-for-Kotlin-practice`.

## Inspect the CLI

Windows PowerShell:

```powershell
python ".agents\hooks\test_repair.py" --help
```

macOS or Linux:

```bash
python3 .agents/hooks/test_repair.py --help
```

The setup is complete when `--help` lists `refresh`, `show`, `lock`, `unlock`,
and `complete`.

## Process one failure

Windows PowerShell:

```powershell
python ".agents\hooks\test_repair.py" refresh
python ".agents\hooks\test_repair.py" show
python ".agents\hooks\test_repair.py" lock
```

macOS or Linux:

```bash
python3 .agents/hooks/test_repair.py refresh
python3 .agents/hooks/test_repair.py show
python3 .agents/hooks/test_repair.py lock
```

## Record the triage decision

The script selects and locks the exact failed test. Only one repair lock may be
active in the repository; unlock or complete it before locking another. The
script does not diagnose the failure. Read the matching failure digest and
record one triage outcome:

- `PRODUCT_BUG`: the application or backend under test is wrong;
- `TEST_AUTOMATION_BUG`: the test, its data, setup, or configuration is wrong;
- `INFRASTRUCTURE_ISSUE`: the SDK, runner, emulator, device, Appium, or network
  is unavailable or misconfigured;
- `NEEDS_INVESTIGATION`: the available evidence does not distinguish the
  causes yet.

Record reproduction separately:

- `DETERMINISTIC`: the same conditions produce the same result;
- `FLAKY`: the result changes with the same code and conditions;
- `NOT_VERIFIED`: reproduction has not been checked yet.

`FLAKY` is not a cause or a fifth triage outcome. A flaky result may come from
the product, test automation, or infrastructure.

Use the `id` returned by `lock` when work is interrupted or must return to the
queue:

```powershell
python ".agents\hooks\test_repair.py" unlock --id <id>
```

On macOS or Linux, run the same command with `python3` and forward slashes.

The script reads test evidence from the Kotlin project and writes generated
queue state to `.agent-state/`. It does not diagnose a failure or edit Kotlin
code by itself; the coding agent performs that work through the `test-repair`
skill, including the exact Gradle run and any evidence-backed test-layer fix.
Commit the hook and canonical skill to the Kotlin repository after the lesson.
Do not commit `.agent-state/`, the prepared failure test, or generated test
evidence.
