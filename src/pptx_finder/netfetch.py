"""多源下载的公共部件：人话原因、拦截页识别、速度/剩余时间、按顺序逐源回落。

自动更新（updater.py）和识别组件下载（imgtext_ocr.py / 公式组件）共用这一套，
照搬 AI Hub 一键更新（2026-10-13）里验证过的做法：
- 每个源下完都核对大小和 sha256，不对就写明原因、换下一个源；
- 公司代理会把大文件换成几 KB 的拦截网页（Hub 公司实测：7 MB 的包只收到 3.5 KB），
  这类情况要认出来并说人话，而不是报「哈希校验失败」让人摸不着头脑；
- 进度带上当前源、速度、剩余时间，界面据此显示。

纯标准库，不依赖 Qt，便于单测。
"""
from __future__ import annotations

import hashlib
import http.client
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_BUF = 1 << 16
#: 小于这个大小、内容又像网页的响应，基本就是代理/网关的拦截页
_WEB_PAGE_MAX = 256 * 1024

#: 内部源标识 → 界面上显示的名字
SOURCE_NAMES = {
    "github-release": "GitHub",
    "self-hosted": "备用服务器",
}


def source_name(label: str) -> str:
    if label in SOURCE_NAMES:
        return SOURCE_NAMES[label]
    if label.startswith("self-hosted"):
        return "备用服务器" + label[len("self-hosted"):].replace("-", " ")
    return label


def fmt_bytes(n: float) -> str:
    n = float(n or 0)
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{int(n)} B"


def fmt_eta(seconds) -> str:
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    if seconds >= 60:
        return f"剩约 {-(-seconds // 60)} 分钟"
    return f"剩约 {max(1, seconds)} 秒"


class SourceFailure(ValueError):
    """某个源这一次不可用；reason 是给用户看的人话。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AllSourcesFailed(RuntimeError):
    """所有源都试过了。attempts 里有每个源的结果，界面逐条列出。"""

    def __init__(self, what: str, attempts: list):
        self.attempts = list(attempts)
        detail = "；".join(f"{a.name}：{a.reason}" for a in self.attempts if a.reason)
        super().__init__(f"{what}失败：{detail}" if detail else f"{what}失败")


def looks_like_web_page(content_type: str | None, head: bytes | None) -> bool:
    ct = (content_type or "").lower()
    if any(t in ct for t in ("text/html", "application/xhtml")):
        return True
    if head:
        s = head[:256].lstrip().lower()
        return s.startswith((b"<!doctype", b"<html", b"<head", b"<?xml", b"<body"))
    return False


def web_page_reason(size: int) -> str:
    return f"收到 {fmt_bytes(size)} 的网页而不是更新文件，可能被公司代理拦截"


def reason_of(exc: BaseException) -> str:
    """把各种网络/校验异常翻译成一句人话。"""
    if isinstance(exc, SourceFailure):
        return exc.reason
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return "源上没有这个文件（HTTP 404）"
        if exc.code in (407,):
            return "代理要求登录（HTTP 407）"
        if exc.code == 403:
            return "服务器拒绝访问（HTTP 403）"
        return f"服务器返回 HTTP {exc.code}"
    inner = exc
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        inner = exc.reason
    if isinstance(inner, (socket.timeout, TimeoutError)):
        return "连接超时（服务器长时间没有响应）"
    if isinstance(inner, socket.gaierror):
        return "找不到服务器（域名解析失败），可能需要代理或网络不通"
    if isinstance(inner, ssl.SSLError) or "CERTIFICATE" in str(inner).upper():
        return "证书校验失败（可能被公司代理拦截了加密连接）"
    if isinstance(inner, ConnectionRefusedError):
        return "服务器拒绝连接"
    if isinstance(inner, (ConnectionResetError, ConnectionAbortedError,
                          http.client.RemoteDisconnected, http.client.IncompleteRead)):
        return "连接被中断（可能被代理或防火墙重置）"
    msg = str(inner or exc)
    low = msg.lower()
    if "timed out" in low or "timeout" in low:
        return "连接超时（服务器长时间没有响应）"
    if "tunnel connection failed" in low or "proxy" in low:
        return "代理连接失败"
    if "getaddrinfo" in low or "name or service not known" in low or "11001" in low:
        return "找不到服务器（域名解析失败），可能需要代理或网络不通"
    if "哈希校验失败" in msg or "校验失败" in msg:
        return "文件校验不一致（内容在传输中被改动，或源上的文件不对）"
    if isinstance(inner, OSError) and getattr(inner, "winerror", None) in (10051, 10065):
        return "网络不通"
    return (msg or type(exc).__name__)[:160]


class ProgressMeter:
    """平滑速度（指数滑动平均）与剩余时间；每 250ms 最多出一次快照。"""

    def __init__(self, total: int, source: str = "", *, clock=time.monotonic,
                 interval: float = 0.25):
        self.total = max(0, int(total or 0))
        self.source = source
        self.received = 0
        self.speed = 0.0
        self._clock = clock
        self._interval = interval
        self._last_t = clock()
        self._last_b = 0

    def update(self, received: int, total: int | None = None) -> dict | None:
        if total:
            self.total = int(total)
        self.received = int(received)
        now = self._clock()
        dt = now - self._last_t
        finished = self.total and self.received >= self.total
        if dt < self._interval and not finished:
            return None
        if dt > 0:
            inst = (self.received - self._last_b) / dt
            self.speed = inst if self.speed <= 0 else self.speed * 0.7 + inst * 0.3
        self._last_t, self._last_b = now, self.received
        return self.snapshot()

    def eta(self):
        if self.speed <= 0 or not self.total:
            return None
        return max(0, round((self.total - self.received) / self.speed))

    def snapshot(self) -> dict:
        return {"source": self.source, "received": self.received, "total": self.total,
                "speed": self.speed, "eta": self.eta()}


def describe_progress(status: dict, what: str) -> str:
    """「从 GitHub 下载识别组件 12.3 / 80.7 MB · 2.1 MB/s · 剩约 30 秒」"""
    src = status.get("source") or ""
    head = f"从 {src} 下载{what}" if src else f"下载{what}"
    received, total = status.get("received", 0), status.get("total", 0)
    parts = [f"{head} {received / 1048576:.1f} / {total / 1048576:.1f} MB" if total
             else f"{head} {fmt_bytes(received)}"]
    if status.get("speed"):
        parts.append(f"{fmt_bytes(status['speed'])}/s")
    eta = fmt_eta(status.get("eta"))
    if eta:
        parts.append(eta)
    return " · ".join(parts)


@dataclass
class Attempt:
    """一个源这一次的结果，给界面逐条显示。"""

    label: str
    name: str
    url: str = ""
    status: str = "downloading"   # downloading / ok / failed / cancelled
    reason: str = ""
    bytes: int = 0
    ms: int = 0
    started: float = field(default_factory=time.monotonic)

    def finish(self, status: str, reason: str = "", nbytes: int = 0) -> None:
        self.status = status
        self.reason = reason
        self.bytes = nbytes
        self.ms = int((time.monotonic() - self.started) * 1000)

    def as_dict(self) -> dict:
        return {"label": self.label, "name": self.name, "status": self.status,
                "reason": self.reason, "bytes": self.bytes, "ms": self.ms}


def check_response_is_not_page(response, expect_size: int | None, what: str = "更新文件") -> None:
    """拿到响应头就先看一眼：明显是网页的不往下读那么多。"""
    headers = getattr(response, "headers", None)
    ctype = ""
    length = None
    if headers is not None:
        try:
            ctype = headers.get("Content-Type") or ""
            raw_len = headers.get("Content-Length")
            length = int(raw_len) if raw_len not in (None, "") else None
        except (TypeError, ValueError, AttributeError):
            pass
    if looks_like_web_page(ctype, None):
        body = b""
        try:
            body = response.read(_WEB_PAGE_MAX)
        except Exception:  # noqa: BLE001
            pass
        size = length if length is not None else len(body)
        raise SourceFailure(f"收到 {fmt_bytes(size)} 的网页而不是{what}，可能被公司代理拦截")
    if expect_size and length is not None and length != expect_size and length < _WEB_PAGE_MAX:
        body = b""
        try:
            body = response.read(_WEB_PAGE_MAX)
        except Exception:  # noqa: BLE001
            pass
        if looks_like_web_page(ctype, body):
            raise SourceFailure(f"收到 {fmt_bytes(len(body) or length)} 的网页而不是{what}，可能被公司代理拦截")
        raise SourceFailure(
            f"文件大小不对：应为 {fmt_bytes(expect_size)}，服务器给的是 {fmt_bytes(length)}")


def download_file(url: str, dest: Path, *, expect_size: int | None, expect_sha256: str,
                  timeout: float = 30, cancel=None, on_bytes=None,
                  response_callback=None, what: str = "更新文件") -> int:
    """下载一个文件到 dest（先写 .part，核对大小与 sha256 通过才改名）。返回字节数。

    取消抛 InterruptedError；源的问题抛 SourceFailure（带人话原因）；其余网络异常原样抛，
    由调用方 reason_of 翻译。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if cancel is not None and cancel():
        raise InterruptedError("下载已取消")
    hasher = hashlib.sha256()
    received = 0
    head = b""
    response = urllib.request.urlopen(url, timeout=timeout)
    try:
        with response as r:
            if response_callback is not None:
                response_callback(r)
            check_response_is_not_page(r, expect_size, what)
            headers = getattr(r, "headers", None)
            ctype = ""
            try:
                ctype = (headers.get("Content-Type") if headers is not None else "") or ""
            except AttributeError:
                ctype = ""
            with open(part, "wb") as out:
                while True:
                    chunk = r.read(_BUF)
                    if not chunk:
                        break
                    if not head:
                        head = chunk[:256]
                    received += len(chunk)
                    if expect_size and received > expect_size:
                        raise SourceFailure(
                            f"收到的内容比应有的 {fmt_bytes(expect_size)} 还大，已停止")
                    out.write(chunk)
                    hasher.update(chunk)
                    if on_bytes is not None:
                        on_bytes(received)
                    if cancel is not None and cancel():
                        raise InterruptedError("下载已取消")
        if response_callback is not None:
            response_callback(None)
        if expect_size and received != expect_size:
            if received < _WEB_PAGE_MAX and looks_like_web_page(ctype, head):
                raise SourceFailure(f"收到 {fmt_bytes(received)} 的网页而不是{what}，可能被公司代理拦截")
            raise SourceFailure(
                f"文件不完整：应为 {fmt_bytes(expect_size)}，只收到 {fmt_bytes(received)}")
        if hasher.hexdigest() != str(expect_sha256).lower():
            if received < _WEB_PAGE_MAX and looks_like_web_page(ctype, head):
                raise SourceFailure(f"收到 {fmt_bytes(received)} 的网页而不是{what}，可能被公司代理拦截")
            raise SourceFailure("文件校验不一致（内容在传输中被改动，或源上的文件不对）")
        os.replace(part, dest)
        return received
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def download_with_fallback(candidates: list[tuple[str, str]], dest: Path, *,
                           expect_size: int | None, expect_sha256: str,
                           timeout: float = 30, cancel=None, on_status=None,
                           on_attempts=None, response_callback=None,
                           what: str = "更新文件") -> tuple[Attempt, list[Attempt]]:
    """candidates: [(label, url)] 按顺序。成功返回 (成功的 attempt, 全部 attempts)。

    on_status(dict) 收进度快照（含源名、已收/总、速度、剩余秒数）；on_attempts(list[dict])
    在每个源开始/结束时收到全部源的结果。全部失败抛 AllSourcesFailed。
    """
    attempts: list[Attempt] = []

    def _emit():
        if on_attempts is not None:
            on_attempts([a.as_dict() for a in attempts])

    for label, url in candidates:
        if cancel is not None and cancel():
            raise InterruptedError("下载已取消")
        attempt = Attempt(label=label, name=source_name(label), url=url)
        attempts.append(attempt)
        _emit()
        meter = ProgressMeter(expect_size or 0, attempt.name)

        def _on_bytes(n, meter=meter):
            snap = meter.update(n)
            if snap is not None and on_status is not None:
                on_status(snap)

        try:
            n = download_file(url, dest, expect_size=expect_size, expect_sha256=expect_sha256,
                              timeout=timeout, cancel=cancel, on_bytes=_on_bytes,
                              response_callback=response_callback, what=what)
        except InterruptedError:
            attempt.finish("cancelled", "已取消")
            _emit()
            raise
        except Exception as exc:  # noqa: BLE001 一个源的任何问题都换下一个
            attempt.finish("failed", reason_of(exc))
            _emit()
            continue
        attempt.finish("ok", "", n)
        _emit()
        return attempt, attempts
    raise AllSourcesFailed(f"下载{what}", attempts)
