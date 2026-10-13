# -*- coding: utf-8 -*-
r"""把一个已发布版本同步到自建更新源（ECS 上的 Caddy 静态目录），作为 GitHub 之外的备用源。

为什么要它：v2.0 起客户端按「GitHub → 备用服务器」逐源回落（照 AI Hub 一键更新）。备用
服务器上的清单此前长期停在 v1.2.7、识别组件目录 404——只有每次发版都同步上去，备用源才真
的能兜住「GitHub 连不上 / 被代理拦截」的用户。

用法（上传一律等田哥点头才跑）：
    uv run --with paramiko python tools/mirror_release.py 2.0.0 --dry-run
    $env:ECS_PWD="..."; uv run --with paramiko python tools/mirror_release.py 2.0.0
    ...                                   --components        # 顺带同步 OCR / 公式组件
    ...                                   --from-dir <目录>   # 用本地产物，不从 GitHub 下

资产来源默认是 GitHub Release `v<版本>` 的资产（与客户端从 GitHub 拿到的逐字节相同）；
本地缓存在 artifacts/mirror-cache/v<版本>/，重跑会跳过已下好且大小对的文件。

服务器布局（https://me.lt-stockpartner.tech/pptutor ↔ C:/pptutor-updates）：
    manifest.json / manifest-Base.json / manifest-Ocr.json      三个安装版各自的清单
    files/<sha256>                                              增量块（内容寻址）
    PPT-Doctor-v<版本>[-Base|-Ocr].zip                          整包兜底
    ocr/component.json + ocr/component.zip                      文字识别组件
    formula/component.json + formula/component.zip              公式识别组件

顺序：块和整包 → 组件 → 最后才传清单（客户端永远看不到指向缺失文件的清单）。
上传后从公网逐个下载，核对大小与 sha256；任何一项不一致，退出码非 0。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

HOST = "8.130.158.231"
USER = "Administrator"
REMOTE = "C:/pptutor-updates"
PUBLIC = "https://me.lt-stockpartner.tech/pptutor"
REPO = "TianLin0509/PPTutor"
COMPONENT_TAGS = {"ocr": "ocr-v1.1.0", "formula": "formula-v1.0.0"}
MANIFESTS = ("manifest.json", "manifest-Base.json", "manifest-Ocr.json")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class Item:
    local: Path
    remote: str          # 相对 REMOTE 的路径（正斜杠）
    size: int
    sha256: str
    last: bool = False   # 清单：最后传

    @property
    def url(self) -> str:
        return f"{PUBLIC}/{self.remote}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _github_assets(tag: str) -> list[dict]:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/tags/{tag}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "pptdoctor-mirror"})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8")).get("assets", [])


def _download(url: str, dest: Path, size: int) -> None:
    if dest.is_file() and dest.stat().st_size == size:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    done = 0
    t0 = time.monotonic()
    with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if size > 20 << 20 and done % (50 << 20) < (1 << 20):
                rate = done / max(0.1, time.monotonic() - t0) / 1048576
                print(f"    {dest.name}: {done / 1048576:.0f}/{size / 1048576:.0f} MB · {rate:.1f} MB/s")
    if part.stat().st_size != size:
        raise SystemExit(f"[!] {dest.name} 下载不完整：{part.stat().st_size} != {size}")
    os.replace(part, dest)


def collect_from_github(version: str, cache: Path, components: bool,
                        download: bool = True) -> list[Item]:
    """download=False（--dry-run）只按 GitHub 的资产元数据列清单，不下载几百 MB 的包。"""
    items: list[Item] = []
    print(f"[*] 读取 GitHub Release v{version} 的资产清单")
    for asset in _github_assets(f"v{version}"):
        name = asset["name"]
        if name in MANIFESTS:
            remote, last = name, True
        elif _HEX64.fullmatch(name):
            remote, last = f"files/{name}", False
        elif name.startswith(f"PPT-Doctor-v{version}") and name.endswith(".zip"):
            remote, last = name, False
        else:
            continue          # 安装器 exe、SHA256SUMS 之类不是更新器要的
        dest = cache / name
        if download:
            _download(asset["browser_download_url"], dest, asset["size"])
        items.append(Item(dest, remote, asset["size"], _sha256(dest) if download else "", last))
    if components:
        for kind, tag in COMPONENT_TAGS.items():
            print(f"[*] 读取组件 {tag}")
            for asset in _github_assets(tag):
                if asset["name"] not in ("component.json", "component.zip"):
                    continue
                dest = cache / kind / asset["name"]
                if download:
                    _download(asset["browser_download_url"], dest, asset["size"])
                # component.json 最后传：它指向的 zip 先到位
                items.append(Item(dest, f"{kind}/{asset['name']}", asset["size"],
                                  _sha256(dest) if download else "",
                                  asset["name"] == "component.json"))
    return items


def collect_from_dir(version: str, src: Path, components: bool) -> list[Item]:
    """本地产物目录：update-assets 的平铺块 + 清单 + 整包 zip；组件放 ocr/ formula/ 子目录。"""
    items: list[Item] = []
    for p in sorted(src.iterdir()):
        if not p.is_file():
            continue
        if p.name in MANIFESTS:
            items.append(Item(p, p.name, p.stat().st_size, _sha256(p), True))
        elif _HEX64.fullmatch(p.name):
            items.append(Item(p, f"files/{p.name}", p.stat().st_size, _sha256(p)))
        elif p.name.startswith(f"PPT-Doctor-v{version}") and p.suffix == ".zip":
            items.append(Item(p, p.name, p.stat().st_size, _sha256(p)))
    if components:
        for kind in COMPONENT_TAGS:
            for name in ("component.zip", "component.json"):
                p = src / kind / name
                if p.is_file():
                    items.append(Item(p, f"{kind}/{name}", p.stat().st_size, _sha256(p),
                                      name == "component.json"))
    return items


def check_consistency(items: list[Item]) -> None:
    """清单里引用的块 sha256 必须与块文件一致；清单版本必须一致。"""
    by_remote = {i.remote: i for i in items}
    versions = set()
    for item in items:
        if item.remote in MANIFESTS:
            data = json.loads(item.local.read_text("utf-8"))
            versions.add(str(data.get("version")))
    if len(versions) > 1:
        raise SystemExit(f"[!] 三个清单版本不一致：{versions}")
    for remote, item in by_remote.items():
        if remote.startswith("files/") and remote[6:] != item.sha256:
            raise SystemExit(f"[!] 块 {remote} 的内容哈希对不上文件名")


def _connect():
    import paramiko  # noqa: PLC0415 只有真上传才需要
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pwd = os.environ.get("ECS_PWD")
    key = Path(os.environ.get("ECS_KEY", Path.home() / ".ssh" / "codex_aliyun"))
    if pwd:
        client.connect(HOST, 22, USER, pwd, timeout=20)
    elif key.is_file():
        client.connect(HOST, 22, USER, key_filename=str(key), look_for_keys=False,
                       allow_agent=False, timeout=20)
    else:
        raise SystemExit("[!] 需要环境变量 ECS_PWD（或 ECS_KEY 指向私钥）")
    return client


def _ensure_dir(sftp, path: str) -> None:
    parts = path.split("/")
    for i in range(2, len(parts) + 1):
        sub = "/".join(parts[:i])
        try:
            sftp.stat(sub)
        except OSError:
            sftp.mkdir(sub)


def upload(items: list[Item]) -> None:
    client = _connect()
    sftp = client.open_sftp()
    ordered = sorted(items, key=lambda i: i.last)
    total = sum(i.size for i in ordered)
    sent = 0
    for n, item in enumerate(ordered, 1):
        remote = f"{REMOTE}/{item.remote}"
        _ensure_dir(sftp, remote.rsplit("/", 1)[0])
        try:
            same = sftp.stat(remote).st_size == item.size and not item.last \
                and item.remote.startswith(("files/", "PPT-Doctor-v"))
        except OSError:
            same = False
        if same:
            print(f"  [{n}/{len(ordered)}] 已存在，跳过 {item.remote}")
            continue
        tmp = remote + ".uploading"
        sftp.put(str(item.local), tmp)
        try:
            sftp.remove(remote)
        except OSError:
            pass
        sftp.rename(tmp, remote)
        sent += item.size
        print(f"  [{n}/{len(ordered)}] {item.remote}  {item.size / 1048576:.1f} MB"
              f"（累计 {sent / 1048576:.0f}/{total / 1048576:.0f} MB）")
    sftp.close()
    client.close()


def verify_public(items: list[Item]) -> int:
    bad = 0
    for item in items:
        h = hashlib.sha256()
        got = 0
        try:
            req = urllib.request.Request(item.url, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=60) as r:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
                    got += len(chunk)
            ok = got == item.size and h.hexdigest() == item.sha256
            why = "" if ok else f"大小 {got}/{item.size}，sha256 {'一致' if h.hexdigest() == item.sha256 else '不一致'}"
        except Exception as exc:  # noqa: BLE001
            ok, why = False, f"{type(exc).__name__}: {exc}"
        bad += not ok
        print(f"  {'OK ' if ok else 'BAD'} {item.remote} {why}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="把已发布版本同步到自建更新源（备用服务器）")
    ap.add_argument("version", nargs="?", help="版本号，默认取源码里的 __version__")
    ap.add_argument("--from-dir", type=Path, help="用本地产物目录，不从 GitHub 下载")
    ap.add_argument("--components", action="store_true", help="顺带同步 OCR / 公式组件")
    ap.add_argument("--dry-run", action="store_true", help="只列出将上传的清单，不连服务器")
    ap.add_argument("--verify-only", action="store_true", help="不上传，只核对公网上的文件")
    args = ap.parse_args()
    if not args.version:
        from pptx_finder import __version__
        args.version = __version__

    if args.from_dir:
        items = collect_from_dir(args.version, args.from_dir, args.components)
    else:
        cache = ROOT / "artifacts" / "mirror-cache" / f"v{args.version}"
        items = collect_from_github(args.version, cache, args.components,
                                    download=not args.dry_run)
    if not any(i.remote in MANIFESTS for i in items):
        print("[!] 没找到任何清单（manifest*.json），版本号对吗？")
        return 2
    if all(i.sha256 for i in items):
        check_consistency(items)

    print(f"\n[plan] 同步 v{args.version} → {PUBLIC}（{REMOTE}）")
    for item in sorted(items, key=lambda i: (i.last, i.remote)):
        tag = "（最后）" if item.last else ""
        print(f"  {item.size / 1048576:9.2f} MB  {item.remote}{tag}")
    print(f"  合计 {len(items)} 个文件 / {sum(i.size for i in items) / 1048576:.1f} MB")
    if args.dry_run:
        print("\n[dry-run] 未连接服务器。")
        return 0
    if not args.verify_only:
        print("\n[upload]")
        upload(items)
    print("\n[verify] 从公网逐个下载核对大小与 sha256")
    bad = verify_public(items)
    print("\n[done]" if not bad else f"\n[!] {bad} 个文件公网核对不一致")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
