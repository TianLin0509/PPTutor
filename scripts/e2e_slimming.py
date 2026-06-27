"""E2E check for PPT slimming.

Creates an isolated PPTX, injects typical bloat, drives the SlimWindow flow in
offscreen Qt, and verifies no PowerPoint process is started by the slimming path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from lxml import etree  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import fixtures_gen as fx  # noqa: E402
from pptx_finder import slim  # noqa: E402
from pptx_finder.ui import slim_window, theme  # noqa: E402

RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
    b"\xff?\x00\x05\xfe\x02\xfeA\xe2&\xb5\x00\x00\x00\x00IEND\xaeB`\x82"
)


def powerpoint_count() -> int:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE", "/FO", "CSV", "/NH"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return -1
    if "INFO:" in out:
        return 0
    return sum(1 for line in out.splitlines() if "POWERPNT.EXE" in line.upper())


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(pred, timeout_ms: int = 5000) -> None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        pump(30)
        if pred():
            return
    raise TimeoutError("condition timed out")


def inject_bloat(path: Path) -> None:
    with zipfile.ZipFile(path) as zin:
        data = {n: zin.read(n) for n in zin.namelist()}
    ct = etree.fromstring(data["[Content_Types].xml"])
    if not any(
        child.tag == f"{{{CT_NS}}}Default" and (child.get("Extension") or "").lower() == "png"
        for child in ct
    ):
        node = etree.SubElement(ct, f"{{{CT_NS}}}Default")
        node.set("Extension", "png")
        node.set("ContentType", "image/png")
        data["[Content_Types].xml"] = etree.tostring(ct, xml_declaration=True, encoding="UTF-8", standalone=True)
    data["ppt/media/e2eA.png"] = PNG * 80
    data["ppt/media/e2eB.png"] = PNG * 80
    data["ppt/media/e2e-orphan.png"] = PNG * 50
    data["__MACOSX/._e2e"] = b"junk"
    rels_name = "ppt/slides/_rels/slide1.xml.rels"
    rels = etree.fromstring(data[rels_name])
    for idx, target in enumerate(("../media/e2eA.png", "../media/e2eB.png"), start=70):
        rel = etree.SubElement(rels, f"{{{RELS_NS}}}Relationship")
        rel.set("Id", f"rIdE2E{idx}")
        rel.set("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image")
        rel.set("Target", target)
    data[rels_name] = etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True)
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, blob in data.items():
            zout.writestr(name, blob)
    tmp.replace(path)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="pptdoctor_slim_e2e_"))
    deck = work / "source.pptx"
    out = work / "source.slim.pptx"
    fx.make_pptx(deck, [{"body": "PPT Doctor 瘦身 E2E"}, {"body": "第二页"}])
    inject_bloat(deck)

    before_ppt = powerpoint_count()
    app = QApplication.instance() or QApplication(sys.argv)
    messages = []
    slim_window.QFileDialog = SimpleNamespace(getSaveFileName=lambda *args, **kwargs: (str(out), ""))
    slim_window.QMessageBox = SimpleNamespace(
        information=lambda *args, **kwargs: messages.append(("info", args)),
        warning=lambda *args, **kwargs: messages.append(("warn", args)),
    )
    win = slim_window.SlimWindow(theme.tok("cloud"), str(deck))
    wait_until(lambda: win._report is not None)
    win._make_slim_copy()
    wait_until(lambda: out.exists() and bool(messages))
    after_ppt = powerpoint_count()

    report_before = slim.analyze_pptx(str(deck))
    report_after = slim.analyze_pptx(str(out))
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())

    result = {
        "ok": True,
        "source": str(deck),
        "output": str(out),
        "source_size": deck.stat().st_size,
        "output_size": out.stat().st_size,
        "duplicate_groups_before": len(report_before.duplicate_media_groups),
        "duplicate_groups_after": len(report_after.duplicate_media_groups),
        "orphan_present_after": "ppt/media/e2e-orphan.png" in names,
        "junk_present_after": "__MACOSX/._e2e" in names,
        "powerpoint_count_before": before_ppt,
        "powerpoint_count_after": after_ppt,
        "powerpoint_count_unchanged": before_ppt == after_ppt,
        "messages": [m[0] for m in messages],
    }
    result["ok"] = (
        out.exists()
        and result["duplicate_groups_before"] >= 1
        and result["duplicate_groups_after"] == 0
        and not result["orphan_present_after"]
        and not result["junk_present_after"]
        and result["powerpoint_count_unchanged"]
    )
    artifact = ROOT / "artifacts" / "e2e_slimming.json"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    app.quit()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
