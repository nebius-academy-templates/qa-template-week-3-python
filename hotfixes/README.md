# Appium failure-digest hotfix

Apply this compatibility patch to an existing `AI-for-Kotlin-practice`
checkout before the mobile triage exercise. Do not clone the Kotlin repository
again. The patch preserves the full raw evidence and makes only the compact
failure digest semantic and readable.

The patch applies to the Kotlin template at commit
`46391891a494e06c8cbe329280d660845aa208ca`. It was produced from the verified
`ai-for-qa-kotlin` fix at commit
`1b0ce7f77b879483b9d5bda969438031b3b37aad`.

## Apply

The commands below assume the Kotlin and Week 3 repositories are sibling
directories. Adjust the patch path if they are stored elsewhere.

Run from the root of `AI-for-Kotlin-practice`:

```bash
git apply --check ../qa-template-week-3-python/hotfixes/appium-failure-digest-v2.patch
git apply ../qa-template-week-3-python/hotfixes/appium-failure-digest-v2.patch
```

`git apply --check` changes no files. If it fails, check whether the patch is
already present:

```bash
git apply --reverse --check ../qa-template-week-3-python/hotfixes/appium-failure-digest-v2.patch
```

If the reverse check succeeds, no action is required. If both checks fail, do
not force the patch over local framework changes. Inspect the conflicting files
and keep using the raw logcat, screenshot, page source, JUnit XML, and Allure
attachments until the conflict is resolved.

## Verify

Windows PowerShell:

```powershell
.\gradlew.bat :appium-tests:failureDigestTest
```

macOS or Linux:

```bash
./gradlew :appium-tests:failureDigestTest
```

The task must report eight passing `FailureLogcatTest` cases.

## Included changes

- replace raw 240-character Appium response truncation with a semantic error;
- omit base64 screenshot responses from the compact digest;
- collapse repeated element polling;
- preserve both ends of other long log lines;
- capture logcat before requesting the failure screenshot;
- avoid a duplicate step screenshot when a step fails;
- add focused formatter regression tests.
