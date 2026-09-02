# Week 3 Python: test repair workflow

Use `test_repair.py` to select one failed Kotlin test from Allure results and
keep its triage state explicit. The script uses only the Python standard
library. Do not create a virtual environment or install project dependencies.

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

## Prepare the repositories

Keep this repository and the Kotlin practice repository next to each other:

```text
course-work/
├── AI-for-qa-stu/
└── qa-template-week-3-python/
```

Clone them when they are not already available:

```bash
git clone https://github.com/ai-qa-lab/AI-for-qa-stu.git
git clone https://github.com/nebius-academy-templates/qa-template-week-3-python.git
```

Run the following commands from `qa-template-week-3-python`.

## Inspect the CLI

Windows PowerShell:

```powershell
py -3.11 .\test_repair.py --help
```

macOS or Linux:

```bash
python3 ./test_repair.py --help
```

## Process one failure

Windows PowerShell:

```powershell
py -3.11 .\test_repair.py --project-root ..\AI-for-qa-stu refresh
py -3.11 .\test_repair.py --project-root ..\AI-for-qa-stu show
py -3.11 .\test_repair.py --project-root ..\AI-for-qa-stu claim --worker learner
```

macOS or Linux:

```bash
python3 ./test_repair.py --project-root ../AI-for-qa-stu refresh
python3 ./test_repair.py --project-root ../AI-for-qa-stu show
python3 ./test_repair.py --project-root ../AI-for-qa-stu claim --worker learner
```

Use the `id` returned by `claim` after the triage decision has been reviewed:

```text
python test_repair.py --project-root <path-to-kotlin-project> release --id <id>
```

The script reads test evidence from the Kotlin project and writes generated
queue state to `<path-to-kotlin-project>/.agent-state/`. It does not diagnose a
failure or edit Kotlin code by itself; the coding agent performs that work.
