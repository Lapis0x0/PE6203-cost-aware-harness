"""C2 artifact check script run inside the container (read-only; stdlib plus optional openpyxl / python-docx / python-pptx).

Usage:
  python3 artifact_check.py backup   back up Office files in the working directory at the start and write a manifest
  python3 artifact_check.py check    check Office files created or modified since the start

Plain-text output, one problem per line, or an OK line when there are none. Errors in the script itself must not affect the task, so everything is wrapped in try.
"""
import json
import os
import shutil
import sys
import time
import zipfile
import xml.etree.ElementTree as ET

# These two environment variables are for local testing only and are not set in the container
ROOTS = os.environ.get("CAH_CHECK_ROOTS", ":".join(["/app", os.path.expanduser("~"), "/root", "/home"])).split(":")
EXTS = (".xlsx", ".xlsm", ".docx", ".pptx", ".odt", ".ods", ".odp")
BACKUP_DIR = os.environ.get("CAH_BACKUP_DIR", "/tmp/.cah_backup")
MANIFEST = os.path.join(BACKUP_DIR, "manifest.json")
MAX_DEPTH = 4
MAX_SIZE = 50 * 1024 * 1024
SKIP_DIRS = {"node_modules", ".cache", ".git", "proc", "sys", ".cah_backup", "site-packages", ".local"}


def office_files():
    seen = set()
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("/").count("/")
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath.count("/") - base_depth >= MAX_DEPTH:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                if f.lower().endswith(EXTS) and not f.startswith("~$") and not f.startswith(".~lock"):
                    p = os.path.realpath(os.path.join(dirpath, f))
                    if p in seen:
                        continue
                    seen.add(p)
                    try:
                        if os.path.getsize(p) <= MAX_SIZE:
                            yield p
                    except OSError:
                        pass


def backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    manifest = {"started_at": time.time(), "files": {}}
    for p in office_files():
        dst = BACKUP_DIR + p
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
            manifest["files"][p] = {"size": os.path.getsize(p), "mtime": os.path.getmtime(p)}
        except OSError:
            pass
    with open(MANIFEST, "w") as fh:
        json.dump(manifest, fh)
    print(f"backed up {len(manifest['files'])} office files")


def check_zip_xml(path):
    problems = []
    if not zipfile.is_zipfile(path):
        return ["not a valid zip container (file is corrupt or not saved in the expected format)"]
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            problems.append(f"corrupt zip member {bad}")
        for name in z.namelist():
            if name.endswith((".xml", ".rels")):
                try:
                    ET.fromstring(z.read(name))
                except ET.ParseError as e:
                    problems.append(f"XML part {name} does not parse: {e}")
    return problems


def check_xlsx(path):
    try:
        import openpyxl
    except ImportError:
        return []
    problems = []
    try:
        wb_f = openpyxl.load_workbook(path)
        wb_v = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:
        return [f"openpyxl cannot open it: {type(e).__name__}: {e}"]
    missing = []
    for ws in wb_f.worksheets:
        wv = wb_v[ws.title]
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    if wv[c.coordinate].value is None:
                        missing.append(f"{ws.title}!{c.coordinate}")
    if missing:
        shown = ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else "")
        problems.append(
            f"{len(missing)} formula cells have no cached value, so a reader that does not recalculate sees them as empty "
            f"({shown}); recalculate and save with LibreOffice"
        )
    return problems


def check_docx(path):
    try:
        import docx
    except ImportError:
        return []
    try:
        d = docx.Document(path)
        for s in d.sections:
            _ = [p.text for p in s.header.paragraphs]
            _ = [p.text for p in s.footer.paragraphs]
        _ = [p.text for p in d.paragraphs]
    except Exception as e:
        return [f"python-docx cannot read it: {type(e).__name__}: {e}"]
    return []


def check_pptx(path):
    try:
        import pptx
    except ImportError:
        return []
    try:
        prs = pptx.Presentation(path)
        for slide in prs.slides:
            for shape in slide.shapes:
                _ = shape.shape_type
    except Exception as e:
        return [f"python-pptx cannot read it: {type(e).__name__}: {e}"]
    return []


def check():
    try:
        manifest = json.load(open(MANIFEST))
    except (OSError, ValueError):
        manifest = {"started_at": 0, "files": {}}
    started = manifest["started_at"]
    originals = manifest["files"]
    lines = []
    n_checked = 0
    for p in office_files():
        orig = originals.get(p)
        try:
            mtime = os.path.getmtime(p)
            size = os.path.getsize(p)
        except OSError:
            continue
        if orig is not None and abs(mtime - orig["mtime"]) < 1e-6 and size == orig["size"]:
            continue
        if orig is None and mtime < started:
            continue
        n_checked += 1
        problems = []
        if p.lower().endswith((".xlsx", ".xlsm", ".docx", ".pptx")):
            problems += check_zip_xml(p)
            if not problems:
                if p.lower().endswith((".xlsx", ".xlsm")):
                    problems += check_xlsx(p)
                elif p.lower().endswith(".docx"):
                    problems += check_docx(p)
                elif p.lower().endswith(".pptx"):
                    problems += check_pptx(p)
        if orig is not None and orig["size"] > 20000 and size < 0.5 * orig["size"]:
            problems.append(
                f"file shrank from {orig['size']} to {size} bytes, content may have been lost; "
                f"the original is backed up at {BACKUP_DIR + p}"
            )
        for pr in problems:
            lines.append(f"PROBLEM {p}: {pr}")
    deleted = [p for p in originals if not os.path.exists(p)]
    for p in deleted:
        lines.append(f"PROBLEM {p}: original file no longer exists; backup at {BACKUP_DIR + p}")
    if not lines:
        lines.append(f"OK: {n_checked} new or modified office files checked, no problems found")
    print("\n".join(lines))


if __name__ == "__main__":
    try:
        {"backup": backup, "check": check}[sys.argv[1]]()
    except Exception as e:
        print(f"artifact check skipped: {type(e).__name__}: {e}")
