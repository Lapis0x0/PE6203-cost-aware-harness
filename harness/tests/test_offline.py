"""Offline tests that start no container and call no model; run in an environment with harbor installed:

  PYTHONPATH=harness .venv-harbor/bin/python harness/tests/test_offline.py

Covers: identical to stock Terminus-2 with all five switches off; C4 and C1 prompt templates; C5 denoising; C2 check script.
"""
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from harbor.agents.terminus_2.terminus_2 import Terminus2

from cah_agent import CostAwareTerminus
from cah_agent.agent import TERMINAL_ANCHOR, _c1_keys

HERE = Path(__file__).resolve().parent
CHECK = HERE.parent / "cah_agent" / "artifact_check.py"
MODEL = "openai/deepseek-ai/deepseek-v4.1-flash"


def make(cls, tmp, **kw):
    return cls(logs_dir=Path(tmp), model_name=MODEL, api_base="http://localhost", **kw)


def test_all_off_matches_original(tmp):
    a = make(Terminus2, tmp)
    b = make(CostAwareTerminus, tmp)
    assert a._prompt_template == b._prompt_template
    noisy = "[123:456:1012/101010.123:ERROR:gpu_init.cc(1)] x\n" + "a" * 20000
    assert a._limit_output_length(noisy) == b._limit_output_length(noisy)
    assert a._get_completion_confirmation_message("T") == b._get_completion_confirmation_message("T")
    assert a._get_error_response_type() == b._get_error_response_type()


def test_prompts(tmp):
    c4 = make(CostAwareTerminus, tmp, c4_domain_notes=True)
    assert "openpyxl does not compute formulas" in c4._prompt_template
    assert c4._prompt_template.index("Environment and tool tips") < c4._prompt_template.index(TERMINAL_ANCHOR)
    c4._prompt_template.format(instruction="X", terminal_state="Y")  # no unescaped braces allowed
    c1 = make(CostAwareTerminus, tmp, c1_native_tools=True, c4_domain_notes="true")
    text = c1._prompt_template.format(instruction="X", terminal_state="Y")
    assert "write_file" in text and "Environment and tool tips" in text and '"keystrokes"' not in text
    assert c1._get_error_response_type() == "tool call"
    assert "call task_complete again" in c1._get_completion_confirmation_message("T")


def test_c1_keys(tmp):
    assert _c1_keys({"keystrokes": "ls -la"}) == "ls -la\n"
    assert _c1_keys({"keystrokes": "ls\n"}) == "ls\n"
    assert _c1_keys({"keystrokes": "ls\\n"}) == "ls\n"
    assert _c1_keys({"keystrokes": "C-c"}) == "C-c"
    assert _c1_keys({"keystrokes": "Escape"}) == "Escape"
    assert _c1_keys({"keystrokes": ":wq", "press_enter": False}) == ":wq"
    assert _c1_keys({"keystrokes": ":wq", "press_enter": "false"}) == ":wq"
    assert _c1_keys({"keystrokes": ""}) == ""


def test_denoise(tmp):
    a = make(CostAwareTerminus, tmp, c5_obs_denoise=True)
    out = a._limit_output_length(
        "ls\n[123:456:1012/101010.123:ERROR:bus.cc(407)] Failed to connect\n"
        "Fontconfig error: no cache\nfile.txt\nwait\nwait\nwait\nwait\n$ "
    )
    assert "Fontconfig" not in out and "bus.cc" not in out
    assert "file.txt" in out and "[previous line repeated 3 more times]" in out
    assert "2 system noise lines" in out


def run_check(mode, roots, backup):
    env = {**os.environ, "CAH_CHECK_ROOTS": roots, "CAH_BACKUP_DIR": backup}
    return subprocess.run([sys.executable, str(CHECK), mode], env=env, capture_output=True, text=True).stdout


def test_artifact_check(tmp):
    try:
        import openpyxl
    except ImportError:
        print("  (openpyxl not installed here, xlsx formula check skipped)")
        openpyxl = None
    work = Path(tmp) / "app"
    work.mkdir()
    backup = str(Path(tmp) / "bk")
    big = work / "big.docx"
    with zipfile.ZipFile(big, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<doc>" + "x" * 60000 + "</doc>")
        z.writestr("word/media/pad.bin", os.urandom(40000))
    print("  ", run_check("backup", str(work), backup).strip())
    assert "OK: 0 " in run_check("check", str(work), backup)

    with zipfile.ZipFile(big, "w") as z:  # shrunk; XML declaration not at the start
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "\n<?xml version='1.0'?><doc/>")
    if openpyxl is not None:
        wb = openpyxl.Workbook()
        wb.active["A1"] = 2
        wb.active["A2"] = "=A1*3"
        wb.save(work / "calc.xlsx")
    (work / "broken.pptx").write_bytes(b"not a zip")
    report = run_check("check", str(work), backup)
    print("   " + report.strip().replace("\n", "\n   "))
    assert "does not parse" in report and "shrank" in report
    assert "broken.pptx: not a valid zip" in report
    if openpyxl is not None:
        assert "formula cells have no cached value" in report


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as tmp:
                fn(tmp)
            print(f"PASS {name}")
