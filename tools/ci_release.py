# -*- coding: utf-8 -*-
r"""发版流水线：构建 → 自检 → 发到 GitHub Release。GitHub Actions 推 tag 时调用，本机也能跑。

由来：v2.0.0～v2.0.4 的打包脚本一直存放在 D:\AI-Artifacts 下，只有项目所有者那台电脑能跑。
2026-10-31 起同事也直接维护主干、自己发版，所以整条流程搬进仓库、改成参数化，
逻辑与 v2.0.4 的 build_release_v204.py / publish_v204.py 逐步对应，没有删减自检。

    uv run python tools/ci_release.py fetch   --work <目录>      下载组件与上几版清单
    uv run python tools/ci_release.py build   --work <目录>      构建三个更新渠道 + 两个安装包 + 冻结自检
    uv run python tools/ci_release.py publish --work <目录>      草稿 → 逐个上传核对 → 公开并设为最新

一次 PyInstaller 构建 → 三个更新渠道（基础 / 文字识别 / 完整）→ 整包 zip、各渠道清单、
相对上几版（PREV_COUNT）的增量块；安装包只出基础版与完整版。
自检全部在隔离数据目录里跑：GIF 录制、打包自检、图片转文字（基础版真实联网下载组件）、公式识别。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
REPO = os.environ.get("GITHUB_REPOSITORY", "TianLin0509/PPTutor")
COMPONENT_TAGS = {"ocr": "ocr-v1.1.0", "formula": "formula-v1.0.0"}
PREV_COUNT = 5
FIXTURES = ROOT / "tools" / "release_fixtures"
EDITIONS = ("base", "ocr", "full")
FLAGS = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == "nt" else 0
_VERSION_TAG = re.compile(r"^v\d+\.\d+\.\d+$")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def say(msg):
    print(msg, flush=True)


def digest(p: Path) -> str:
    with p.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def gh(*args, check=True, timeout=3600):
    r = subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, creationflags=FLAGS, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])} 失败：" + r.stderr.decode("utf-8", "replace")[-1800:])
    return r


def api(path):
    return json.loads(gh("api", path, timeout=120).stdout)


def version():
    from pptx_finder import __version__
    return __version__


# ---------------------------------------------------------------- fetch
def download(url: str, dest: Path, size: int | None = None, sha: str | None = None):
    if dest.is_file() and (size is None or dest.stat().st_size == size) and (sha is None or digest(dest) == sha):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=120) as r, tmp.open("wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            break
        except Exception as e:  # noqa: BLE001
            say(f"   下载重试 {attempt + 1}：{dest.name}：{e}")
            time.sleep(5)
    else:
        raise RuntimeError("下载失败：" + url)
    if size is not None and tmp.stat().st_size != size:
        raise RuntimeError(f"大小不符：{dest.name}")
    if sha is not None and digest(tmp) != sha:
        raise RuntimeError(f"sha256 不符：{dest.name}")
    tmp.replace(dest)


def release_assets(tag):
    rel = api(f"repos/{REPO}/releases/tags/{tag}")
    return {a["name"]: a for a in rel["assets"]}


def cmd_fetch(work: Path):
    """组件与上几版清单都取自 GitHub Release——和老用户从 GitHub 拿到的逐字节相同。"""
    for name, tag in COMPONENT_TAGS.items():
        assets = release_assets(tag)
        for fn in ("component.json", "component.zip"):
            a = assets[fn]
            sha = (a.get("digest") or "").removeprefix("sha256:") or None
            say(f"组件 {name}/{fn} {a['size'] / 1048576:.1f} MB")
            download(a["browser_download_url"], work / "components" / name / fn, a["size"], sha)
    current = "v" + version()
    tags = [r["tag_name"] for r in api(f"repos/{REPO}/releases?per_page=40")
            if _VERSION_TAG.match(r["tag_name"]) and not r["draft"] and r["tag_name"] != current]
    prevs = tags[:PREV_COUNT]
    if not prevs:
        raise RuntimeError("找不到任何上一版，增量块无从计算")
    for tag in prevs:
        assets = release_assets(tag)
        for e in EDITIONS:
            fn = manifest_name(e)
            download(assets[fn]["browser_download_url"], work / "prev" / tag / fn, assets[fn]["size"])
    (work / "prev.json").write_text(json.dumps(prevs), "utf-8")
    say("上几版：" + ", ".join(prevs))


# ---------------------------------------------------------------- build
def manifest_name(edition):
    from pptx_finder.editions import manifest_name as f
    return f(edition)


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def cmd_build(work: Path):
    from pptx_finder.editions import package_suffix
    from pptx_finder.updater import build_manifest
    ver = version()
    out = work / "build-out"
    checks = []

    def run(name, args, env=None, timeout=2400):
        start = time.monotonic()
        say(name + " …")
        with (out / (name + ".log")).open("w", encoding="utf-8") as log:
            r = subprocess.run([str(a) for a in args], cwd=ROOT, env=env, stdout=log,
                               stderr=subprocess.STDOUT, creationflags=FLAGS, timeout=timeout)
        checks.append(dict(name=name, exit=r.returncode, seconds=round(time.monotonic() - start, 2)))
        if r.returncode:
            raise RuntimeError(f"{name} 失败：\n" + (out / (name + ".log")).read_text("utf-8", "replace")[-2500:])
        say(name + " 通过")

    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                      capture_output=True, text=True).stdout.strip():
        raise RuntimeError("工作区有未提交改动，发版必须对应一个确切提交")
    prevs = json.loads((work / "prev.json").read_text("utf-8"))
    olds = {e: [json.loads((work / "prev" / t / manifest_name(e)).read_text("utf-8")) for t in prevs]
            for e in EDITIONS}
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    selftest_set = work / "selftest-set"
    if not selftest_set.exists():
        run("selftest-set", ["uv", "run", "python", "tools/gen_selftest_set.py", selftest_set])
    run("core-build", ["uv", "run", "pyinstaller", "pptx-finder.spec", "--noconfirm",
                       "--distpath", out / "core", "--workpath", out / "pyi-work"])
    core = out / "core" / "PPT-Doctor"
    builder = module("release_builder", ROOT / "tools" / "build_installer.py")
    iscc = builder.find_iscc()
    if not iscc:
        raise RuntimeError("找不到 Inno Setup 的 ISCC.exe")
    release = out / "release"
    installers = out / "installers"
    release.mkdir()
    installers.mkdir()
    variants = []
    for edition in EDITIONS:
        dist = out / "editions" / edition / "PPT-Doctor"
        shutil.copytree(core, dist)
        if edition != "base":
            builder.bundle_ocr(work / "components" / "ocr", dist)
        if edition == "full":
            builder.bundle_ocr(work / "components" / "formula", dist, "formula", "pptdoctor-formula.exe")
        (dist / "_internal" / "edition.json").write_text(json.dumps({"edition": edition}), encoding="utf-8")
        builder.DIST = dist
        if builder.check_dist() != 0:
            raise RuntimeError(f"{edition} 打包内容检查失败")
        manifest = build_manifest(dist, ver, f"PPT Doctor v{ver} {edition}", commit=sha,
                                  built_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        assert manifest["edition"] == edition
        (dist / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        archive = release / f"PPT-Doctor-v{ver}{package_suffix(edition)}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for path in sorted(dist.rglob("*")):
                if path.is_file():
                    z.write(path, "PPT-Doctor/" + path.relative_to(dist).as_posix())
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            assert json.loads(z.read("PPT-Doctor/manifest.json")) == manifest
            for rel, meta in manifest["files"].items():
                blob = z.read("PPT-Doctor/" + rel)
                assert len(blob) == meta["size"] and hashlib.sha256(blob).hexdigest() == meta["hash"], rel
        installer = None
        if edition != "ocr":
            run(edition + "-installer", [iscc, f"/DAppVersion={ver}", "/DEdition=" + edition.title(),
                                         f"/DAppSourceDir={dist}", f"/O{installers}", ROOT / "tools" / "installer.iss"])
            installer = installers / f"PPT-Doctor-Setup-v{ver}-{edition.title()}.exe"
            assert installer.is_file()
            shutil.copy2(installer, release / installer.name)
        shutil.copy2(dist / "manifest.json", release / manifest_name(edition))
        blocks = 0
        for rel, meta in manifest["files"].items():
            if meta["size"] and any(o["files"].get(rel, {}).get("hash") != meta["hash"] for o in olds[edition]):
                block = release / meta["hash"]
                if not block.exists():
                    shutil.copy2(dist / rel, block)
                    blocks += 1
                assert digest(block) == meta["hash"]
        env = dict(os.environ, PPTX_FINDER_DATA_DIR=str(out / ("frozen-" + edition)),
                   APPDATA=str(out / ("roaming-" + edition)), PYTHONIOENCODING="utf-8",
                   PPTX_FINDER_SINGLETON_NAME="pptdoctor-release-selftest-" + edition)
        for key in ("PPTUTOR_OCR_CMD", "PPTUTOR_FORMULA_CMD", "QT_QPA_PLATFORM"):
            env.pop(key, None)
        exe = dist / "PPT-Doctor.exe"
        run(edition + "-gif", [exe, "--gif-selftest", out / (edition + "-gif")], env)
        assert json.loads((out / (edition + "-gif") / "result.json").read_text("utf-8"))["passed"]
        run(edition + "-selftest", [exe, "--selftest", selftest_set, out / (edition + "-selftest.json")], env)
        assert json.loads((out / (edition + "-selftest.json")).read_text("utf-8"))["all_pass"]
        # 基础版不带组件：这一步会真实联网下载识别组件（GitHub 优先、备用服务器兜底）
        run(edition + "-text", [exe, "--imgtext", FIXTURES / "imgtext-clear_mixed.png",
                                out / (edition + "-text.pptx"), "--install-component"], env)
        if edition == "full":
            formula_args = [exe, "--formula", FIXTURES / "formula-nested_fraction.png", out / "formula-test.json"]
            run("full-formula", formula_args, env)
            if not json.loads((out / "formula-test.json").read_text("utf-8")).get("preview"):
                # 预览渲染偶发临时文件被占用（WinError 32）：只重试一次，仍失败就停
                run("full-formula-retry", formula_args, env)
            assert json.loads((out / "formula-test.json").read_text("utf-8")).get("preview"), "公式预览为空"
        variants.append(dict(edition=edition, installer=installer.name if installer else "",
                             zip=archive.name, zip_bytes=archive.stat().st_size,
                             installer_bytes=installer.stat().st_size if installer else 0,
                             files=len(manifest["files"]), new_blocks=blocks))
    assets = [dict(name=p.name, bytes=p.stat().st_size, sha256=digest(p)) for p in sorted(release.iterdir()) if p.is_file()]
    (release / "SHA256SUMS.txt").write_text("".join(f'{a["sha256"]}  {a["name"]}\n' for a in assets), encoding="utf-8")
    state = dict(passed=True, version=ver, source_sha=sha, previous=prevs, variants=variants, checks=checks,
                 release_directory=str(release), assets=assets)
    (out / "release-validation.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    say(json.dumps(dict(version=ver, variants=variants), ensure_ascii=False))


# ---------------------------------------------------------------- publish
def tag_notes(tag):
    """发布说明 = 带注释 tag 的正文（scripts/release.py 写进去的）；第一行是标题。"""
    body = subprocess.run(["git", "tag", "-l", "--format=%(contents)", tag], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()
    body = re.sub(r"-----BEGIN PGP SIGNATURE-----.*", "", body, flags=re.S).strip()
    if not body:
        return f"PPT Doctor {tag}", ""
    title, _, rest = body.partition("\n")
    return title.strip(), rest.strip()


def cmd_publish(work: Path, rehearsal=False):
    """rehearsal=True：演练——建一个不公开的草稿 Release、传完全部文件逐个核对，然后删掉草稿。
    除了「公开并设为最新」这最后一下，和真发布走的是同一段代码（验证流水线本身用）。"""
    out = work / "build-out"
    state = json.loads((out / "release-validation.json").read_text("utf-8"))
    assert state["passed"]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert state["source_sha"] == head, (state["source_sha"], head)
    if rehearsal:
        tag = f"rehearsal-v{state['version']}-{head[:8]}"
        title, notes = f"[演练，会自动删除] PPT Doctor {tag}", ""
    else:
        tag = "v" + state["version"]
        tagged = subprocess.check_output(["git", "rev-list", "-n", "1", tag], cwd=ROOT, text=True).strip()
        assert head == tagged, (head, tagged)
        title, notes = tag_notes(tag)
    if not title.startswith("PPT Doctor"):
        title = f"PPT Doctor {tag} · {title}"
    notes_file = work / "release-notes.md"
    notes_file.write_text(notes or title, "utf-8")
    files = sorted(p for p in Path(state["release_directory"]).iterdir() if p.is_file() and p.stat().st_size)
    if gh("release", "view", tag, "--repo", REPO, "--json", "isDraft", check=False).returncode:
        pin = ["--target", head] if rehearsal else ["--verify-tag"]
        gh("release", "create", tag, "--repo", REPO, "--draft", *pin, "--title", title,
           "--notes-file", str(notes_file), "--latest=false")
    url = json.loads(gh("release", "view", tag, "--repo", REPO, "--json", "apiUrl").stdout)["apiUrl"]
    present = {a["name"]: a for a in api(url)["assets"]}
    pending = [p for p in files if p.name not in present or present[p.name]["size"] != p.stat().st_size
               or (present[p.name].get("digest") and present[p.name]["digest"] != "sha256:" + digest(p))]
    say(f"{len(pending)} 个文件 / {sum(p.stat().st_size for p in pending) / 1048576:.1f} MiB 待上传")
    for p in pending:
        for attempt in range(3):
            if gh("release", "upload", tag, str(p), "--repo", REPO, "--clobber", check=False, timeout=5400).returncode == 0:
                break
            say(f"   上传重试 {attempt + 1}：{p.name}")
        else:
            raise RuntimeError("上传失败：" + p.name)
    assets = {a["name"]: a for a in api(url)["assets"]}
    for p in files:
        a = assets.get(p.name)
        assert a and a["size"] == p.stat().st_size, p.name
        if a.get("digest"):
            assert a["digest"] == "sha256:" + digest(p), p.name
    if rehearsal:
        assert api(url)["draft"], "演练的 Release 必须一直是草稿"
        gh("release", "delete", tag, "--repo", REPO, "--yes")   # 草稿没有真标签，不用清
        say(f"演练通过：{len(files)} 个文件上传并逐个核对，草稿已删除")
        return
    gh("release", "edit", tag, "--repo", REPO, "--title", title, "--notes-file", str(notes_file),
       "--draft=false", "--latest")
    assert api(f"repos/{REPO}/releases/latest")["tag_name"] == tag
    say(f"已发布 {tag}：{len(files)} 个文件逐个核对通过")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=("fetch", "build", "publish"))
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--rehearsal", action="store_true", help="publish 只演练：草稿上传核对后删除")
    a = ap.parse_args(argv)
    a.work.mkdir(parents=True, exist_ok=True)
    work = a.work.resolve()
    if a.step == "publish":
        cmd_publish(work, a.rehearsal)
    else:
        {"fetch": cmd_fetch, "build": cmd_build}[a.step](work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
