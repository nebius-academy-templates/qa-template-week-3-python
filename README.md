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
py -3.11 --version
```

If the Python launcher is unavailable, use:

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

## Add the script to the Kotlin repository

The Kotlin practice repository already exists from the previous course
modules. Clone this repository to obtain the Python implementation:

```bash
git clone https://github.com/nebius-academy-templates/qa-template-week-3-python.git
```

Place `test_repair.py` in the Kotlin practice repository at:

```text
AI-for-qa-stu/.agents/hooks/test_repair.py
```

Run the following commands from the root of `AI-for-qa-stu`.

## Inspect the CLI

Windows PowerShell:

```powershell
py -3 ".agents\hooks\test_repair.py" --help
```

macOS or Linux:

```bash
python3 .agents/hooks/test_repair.py --help
```

The setup is complete when `--help` lists `refresh`, `show`, `claim`,
`release`, and `complete`.

## Process one failure

Windows PowerShell:

```powershell
py -3 ".agents\hooks\test_repair.py" refresh
py -3 ".agents\hooks\test_repair.py" show
py -3 ".agents\hooks\test_repair.py" claim --worker learner
```

macOS or Linux:

```bash
python3 .agents/hooks/test_repair.py refresh
python3 .agents/hooks/test_repair.py show
python3 .agents/hooks/test_repair.py claim --worker learner
```

## Record the triage decision

The script selects and claims the exact failed test. It does not diagnose the
failure. Read the matching failure digest and record one triage outcome:

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

Use the `id` returned by `claim` after the triage decision has been reviewed:

```powershell
py -3 ".agents\hooks\test_repair.py" release --id <id>
```

On macOS or Linux, run the same command with `python3` and forward slashes.

The script reads test evidence from the Kotlin project and writes generated
queue state to `.agent-state/`. It does not diagnose a failure or edit Kotlin
code by itself; the coding agent performs that work. Commit
`.agents/hooks/test_repair.py` to the Kotlin repository after the lesson. Do
not commit `.agent-state/` or generated test evidence.
