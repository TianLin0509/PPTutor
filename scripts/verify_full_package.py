"""在隔离数据目录验证解压后的完整包；不启动主窗口或 PowerPoint。

uv run python scripts/verify_full_package.py --dist <完整包目录> --fixtures <.selftest/set>
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/full-package-smoke"))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    exe = args.dist.resolve() / "PPT-Doctor.exe"
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1200, 675), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(Path(os.environ["WINDIR"]) / "Fonts/msyh.ttc"), 56)
    draw.text((80, 120), "PPT Doctor 离线转字", font=font, fill="black")
    draw.text((80, 240), "完整安装包 2026", font=font, fill="black")
    source = output / "中文 空格输入.png"
    img.save(source)
    dest = output / "可编辑文字.pptx"
    dest.unlink(missing_ok=True)
    report = output / "selftest.json"
    report.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="full-package-data-") as tmp:
        env = dict(os.environ, PPTX_FINDER_DATA_DIR=tmp)
        env.pop("PPTUTOR_OCR_CMD", None)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run([str(exe), "--selftest", str(args.fixtures.resolve()), str(report)],
                       env=env, check=True, timeout=180, creationflags=flags)
        subprocess.run([str(exe), "--imgtext", str(source), str(dest)],
                       env=env, check=True, timeout=180, creationflags=flags)
        assert not (Path(tmp) / "ocr").exists(), "不应该下载组件或覆盖旧下载目录"
        assert (Path(tmp) / "ocr-bundled/pptdoctor-ocr.exe").is_file()
    with zipfile.ZipFile(dest) as z:
        assert z.testzip() is None
        page = ET.fromstring(z.read("ppt/slides/slide1.xml"))
        texts = page.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}t")
        text = " ".join(t.text or "" for t in texts)
        assert "2026" in text and "离线转字" in text, text
    result = {"exe": str(exe), "selftest": json.loads(report.read_text("utf-8")),
              "downloaded_ocr_created": False, "bundled_ocr_expanded": True,
              "editable_text": text, "pptx": str(dest)}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"result": str(output / "result.json"), "text": text}))


if __name__ == "__main__":
    main()
