"""C4 generic environment and tool hints, and the C2 verification gate prompts.

C4 content is the same for every task and contains no task-specific file names, URLs or answers.
Braces in templates are parsed by str.format, so the text here must not contain { }.
"""

DOMAIN_NOTES = """

Environment and tool tips (general advice, applies to every task):
- Look around before acting: check running processes (ps aux), wrapper scripts on PATH, and programs that are already running. If the task names a program or tool, read its --help and any wrapper script first, and prefer its built-in options over building your own workaround.
- A browser or other GUI application may already be running for this task. Reuse it instead of killing it and starting a new instance.
- Office files: prefer the high-level APIs of openpyxl, python-docx and python-pptx over unzipping and editing the XML by hand. Keep a copy of the original file before modifying it.
- openpyxl does not compute formulas: a workbook saved by openpyxl has no cached values for formula cells. After writing formulas, recalculate and save with LibreOffice, e.g. soffice --headless --convert-to xlsx --outdir <dir> <file>, then check the values with openpyxl load_workbook(..., data_only=True).
- If the task asks for a real feature (a pivot table, a chart, a built-in slide type, an application setting), create that feature itself, not a static imitation of its output.
- LibreOffice's Python UNO bindings are usually available to the system python3 (/usr/bin/python3) even when a virtualenv python is first on PATH.
- Many desktop applications write their settings only when they exit normally. Quit them properly instead of killing the process.
"""

VERIFY_CHECKLIST = (
    "Before your solution is graded, verify it independently.\n"
    "1. Re-read the task description and list every explicit requirement (target file or page, exact values, formatting, location, what must not change).\n"
    "2. For each requirement, run a command that inspects the actual final state, not your own intermediate output: re-open saved files with a reader "
    "(for spreadsheets also load with data_only=True so formula cells show their computed values), check that files open without errors, "
    "and for browser tasks check the URL and title of the currently active tab.\n"
    "3. Write a short checklist with PASS or FAIL and the evidence for each requirement.\n"
    "If anything fails, fix it and continue working. Only if every item passes, mark the task complete again."
)
