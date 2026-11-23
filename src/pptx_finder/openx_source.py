"""公司内源（openx）更新源：在公司电脑上，更新清单和文件先从内源取（2026-11-23）。

照搬 AI HUB 公开版 core/app-update/openx-source.js 在公司实测过的做法：
- 内源项目 AI-Hub-Huawei（7938）上，每发一版 PPT Doctor 就发一个版本 ``ppt-doctor-update-<版本>``，
  附件是三份版本清单（manifest.json / manifest-Base.json / manifest-Ocr.json）、本版变化的文件块
  （附件名 = sha256，和 GitHub 发布页同名）和基础版安装包；
- 接口匿名可用（2026-11-23 公司电脑实测：版本列表一次约 2 秒，直连和公司代理都通）：
    版本列表 GET {api}by_project_id?projectId=<id>&currentPage=1&pageSize=N → data.records[{id, version, releaseTime}]
    单个版本 GET {api}by_id?id=<id> → data.binaryPackages[{originalFileName, url}]
- 只接受内源自己域名下的 https 附件地址；下载后照旧按清单核对 sha256；
- 公司外连不上内源：记下「连不上」，5 分钟内不再试，外网用户不被拖慢。

纯标准库，不依赖 Qt，便于单测。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from urllib.parse import quote, urlparse

API = "https://openx.huawei.com/openxApi/project/version/release/query/"
PROJECT_ID = 7938
ALLOWED_HOST = "openx.huawei.com"
RELEASE_PREFIX = "ppt-doctor-update-"

LIST_TIMEOUT = 5.0          # 公司实测列表约 2 秒
LIST_TTL = 10 * 60
UNREACHABLE_TTL = 5 * 60
LIST_PAGE_SIZE = 100
MAX_DETAIL_QUERIES = 8      # 找一个块最多再查几个没缓存过的版本


class OpenxUnavailable(RuntimeError):
    """内源连不上 / 没有这份文件。"""


def enabled(frozen: bool) -> bool:
    """PPTX_FINDER_OPENX=0 关、=1 开；默认只在打包版里开（单测和开发环境不碰网络）。"""
    v = os.environ.get("PPTX_FINDER_OPENX", "").strip()
    if v == "0":
        return False
    if v == "1":
        return True
    return bool(frozen)


def _ver_key(name: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", name)[:4])


def safe_url(u: str) -> str:
    try:
        p = urlparse(str(u or ""))
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    if p.scheme == "https" and (host == ALLOWED_HOST or host.endswith("." + ALLOWED_HOST)):
        return p.geturl()
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise OpenxUnavailable(f"内源要求跳转（HTTP {code}），不跟随")


class OpenxIndex:
    """版本列表与各版本附件的缓存。一个进程一份（模块级 INDEX），线程安全。"""

    def __init__(self, api: str = API, project_id: int = PROJECT_ID, opener=None, clock=time.monotonic):
        self.api = api
        self.project_id = project_id
        self._opener = opener
        self._clock = clock
        self._lock = threading.Lock()
        self._list: list | None = None
        self._list_at = 0.0
        self._down_until = 0.0
        self.down_reason = ""
        self._details: dict = {}

    # ---- 网络 ----
    def _get_json(self, url: str, timeout: float) -> dict:
        opener = self._opener or urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "PPT-Doctor-updater"})
        with opener.open(req, timeout=timeout) as r:
            body = json.loads(r.read(4 * 1024 * 1024).decode("utf-8-sig"))
        if not isinstance(body, dict) or str(body.get("code")) != "0" or not body.get("data"):
            raise OpenxUnavailable("内源返回内容不对")
        return body["data"]

    def releases(self, timeout: float = LIST_TIMEOUT) -> list:
        with self._lock:
            now = self._clock()
            if now < self._down_until:
                raise OpenxUnavailable(self.down_reason or "内源连不上（稍后自动重试）")
            if self._list is not None and now - self._list_at <= LIST_TTL:
                return self._list
        try:
            data = self._get_json(f"{self.api}by_project_id?projectId={self.project_id}"
                                  f"&currentPage=1&pageSize={LIST_PAGE_SIZE}", timeout)
            recs = data.get("records")
            if not isinstance(recs, list):
                raise OpenxUnavailable("内源版本列表格式不对")
        except Exception as e:  # noqa: BLE001 公司外多半 DNS 失败 / 超时
            with self._lock:
                self._down_until = self._clock() + UNREACHABLE_TTL
                self.down_reason = f"公司内源连不上：{type(e).__name__}: {str(e)[:80]}"
            raise OpenxUnavailable(self.down_reason) from e
        with self._lock:
            self._list = [r for r in recs if isinstance(r, dict) and r.get("id") is not None]
            self._list_at = self._clock()
            return self._list

    def update_releases(self, timeout: float = LIST_TIMEOUT) -> list:
        """本产品的更新版本，新到旧。"""
        out = [r for r in self.releases(timeout) if str(r.get("version", "")).startswith(RELEASE_PREFIX)
               and re.fullmatch(r"\d+\.\d+\.\d+", str(r["version"])[len(RELEASE_PREFIX):])]
        return sorted(out, key=lambda r: _ver_key(str(r["version"])[len(RELEASE_PREFIX):]), reverse=True)

    def files_of(self, rec: dict, timeout: float = LIST_TIMEOUT) -> dict:
        rid = rec["id"]
        with self._lock:
            hit = self._details.get(rid)
        if hit is not None:
            return hit
        data = self._get_json(f"{self.api}by_id?id={quote(str(rid))}", timeout)
        pkgs = data.get("binaryPackages") if isinstance(data, dict) else None
        files = {}
        for p in pkgs if isinstance(pkgs, list) else []:
            if not isinstance(p, dict):
                continue
            url = safe_url(p.get("url"))
            name = str(p.get("originalFileName") or "").lower()
            if url and name:
                files[name] = url
        with self._lock:
            self._details[rid] = files
        return files

    # ---- 查找 ----
    def latest_file(self, name: str, timeout: float = LIST_TIMEOUT) -> tuple[str, str]:
        """最新一个更新版本里的某个附件（版本清单用）。返回 (url, 版本号)。"""
        rels = self.update_releases(timeout)
        if not rels:
            raise OpenxUnavailable("内源上还没有 PPT Doctor 更新")
        rec = rels[0]
        url = self.files_of(rec, timeout).get(name.lower())
        if not url:
            raise OpenxUnavailable(f"内源最新版本里没有 {name}")
        return url, str(rec["version"])[len(RELEASE_PREFIX):]

    def find(self, name: str, timeout: float = LIST_TIMEOUT) -> str:
        """在各更新版本里按附件名找（新到旧）；找不到抛 OpenxUnavailable。"""
        key = name.lower()
        queries = 0
        for rec in self.update_releases(timeout):
            with self._lock:
                cached = rec["id"] in self._details
            if not cached:
                if queries >= MAX_DETAIL_QUERIES:
                    break
                queries += 1
            url = self.files_of(rec, timeout).get(key)
            if url:
                return url
        raise OpenxUnavailable(f"内源上没有 {name}")


INDEX = OpenxIndex()
