"""公司内源无感下载记录器（2026-11-25）：

背景：PPT Doctor 更新与识别/公式/素材组件下载默认走内源对象存储直链，速度快且稳定，
但匿名直链不会计入内源社区 PPT-Doctor（8214）的实名下载统计。

做法（双轨无感、零新增依赖、零弹窗）：
1. 主轨下载与 SHA-256 校验完全不变，零额外等待；
2. 仅在下载并校验通过后（检查更新永不触发），在后台守护线程拉起无头 Edge，
   复用本机 Edge 登录态打开内源项目下载页，等单点登录就绪后定位目标版本与附件并触发一次下载动作，
   观测到下载/统计请求落地后立即取消大文件传输并回收进程树与临时目录；
3. 任何失败一律静默降级，绝不报错、不阻塞、不影响离线小工具定位；
   启动前先将待补记项落盘，若用户秒点「立即重启」打断后台记录，新版启动后延迟补记一次。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

RECORD_FILE = "openx-download-record.json"
MAX_RECORDED = 200
MAX_PENDING_ATTEMPTS = 2
MAX_PENDING_QUEUE = 5
PENDING_MAX_AGE_SEC = 24 * 3600
DEFAULT_RESUME_DELAY_SEC = 45.0
DEFAULT_TIMEOUT_SEC = 90.0
DEFAULT_LOGIN_GRACE_SEC = 45.0
START_WAIT_SEC = 45.0
ERROR_PAGE_SEC = 8.0
MAX_PAGES = 10
TEMP_PREFIX = "pptdoctor-openx-"
STALE_TEMP_SEC = 6 * 3600

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LOCK = threading.Lock()
_RUNNING_PIDS: set[int] = set()


def _is_frozen() -> bool:
    import sys
    return bool(getattr(sys, "frozen", False))


def is_active(frozen: bool | None = None, env: dict[str, str] | None = None) -> bool:
    """仅打包版（或显式 PPTX_FINDER_OPENX_RECORD=1）生效；单测和开发环境不碰无头浏览器。"""
    e = os.environ if env is None else env
    flag = str(e.get("PPTX_FINDER_OPENX_RECORD", "")).strip()
    if flag == "0":
        return False
    if flag == "1":
        return True
    from . import openx_source
    return openx_source.enabled(_is_frozen() if frozen is None else frozen)


def find_edge(env: dict[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    roots = [
        e.get("ProgramFiles(x86)", ""),
        e.get("ProgramFiles", ""),
        r"C:\Program Files (x86)",
        r"C:\Program Files",
    ]
    for root in roots:
        if not root:
            continue
        cand = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if cand.is_file():
            return str(cand)
    return ""


def _edge_profile_of(user_data_dir: Path) -> str:
    try:
        data = json.loads((user_data_dir / "Local State").read_text(encoding="utf-8"))
        last = (data.get("profile") or {}).get("last_used")
        if isinstance(last, str) and re.fullmatch(r"(Default|Profile \d{1,4})", last):
            if (user_data_dir / last / "Network" / "Cookies").is_file():
                return last
    except Exception:  # noqa: BLE001
        pass
    return "Default"


def copy_edge_login(tmp_dir: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    e = os.environ if env is None else env
    src = Path(e.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
    info: dict[str, Any] = {"profile": "", "copied": 0, "cookies": False}
    if not src.is_dir():
        return info
    prof = _edge_profile_of(src)
    info["profile"] = prof
    pairs = [
        (src / "Local State", tmp_dir / "Local State"),
        (src / prof / "Network" / "Cookies", tmp_dir / "Default" / "Network" / "Cookies"),
        (src / prof / "Preferences", tmp_dir / "Default" / "Preferences"),
    ]
    for src_f, dst_f in pairs:
        try:
            dst_f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_f, dst_f)
            info["copied"] += 1
            if dst_f.name == "Cookies":
                info["cookies"] = True
        except Exception:  # noqa: BLE001 复制不到就靠安全客户端单点登录
            pass
    return info


def sweep_stale_temp_dirs(tmp_root: Path | None = None, now: float | None = None) -> int:
    root = tmp_root or Path(tempfile.gettempdir())
    ts = time.time() if now is None else now
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for item in entries:
        if not item.name.startswith(TEMP_PREFIX) or not item.is_dir():
            continue
        try:
            if ts - item.stat().st_mtime < STALE_TEMP_SEC:
                continue
            moved = item.with_name(f"{item.name}-sweep{os.getpid()}")
            item.rename(moved)
            shutil.rmtree(moved, ignore_errors=True)
            removed += 1
        except OSError:
            pass
    return removed


def _kill_pid_tree(pid: int) -> None:
    if not pid or pid <= 0:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
                timeout=10,
                check=False,
            )
        else:
            os.kill(pid, 9)
    except Exception:  # noqa: BLE001
        pass


def sanitize_api_path(raw_url: str) -> str:
    try:
        p = urlparse(str(raw_url or "")).path or ""
        if "/openxapi/" not in p.lower():
            return ""
        return re.sub(r"/\d{3,}(?=/|$)", "/:id", p)[:140]
    except Exception:  # noqa: BLE001
        return ""


def is_download_like_url(raw_url: str, target_file: str = "") -> bool:
    try:
        u = urlparse(str(raw_url or ""))
        host = (u.hostname or "").lower()
        p = (u.path or "").lower()
        if "/visitopenxrecord/" in p or "/index/admin/" in p:
            return False
        if target_file and target_file.lower() in p:
            return True
        if host.startswith("file.") or "/openx-file/" in p:
            return True
        if re.search(r"\.(?:gz|exe|zip|7z|msi|bin)$", p):
            return True
        if re.search(r"/openxapi/.*(?:download|record|count|binary|attach)", p):
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


class _CdpClient:
    """纯标准库最小 RFC 6455 WebSocket 客户端，仅连接本机 127.0.0.1 CDP 调试端口。"""

    def __init__(self, ws_url: str, on_event: Callable[[dict], None] | None = None, timeout: float = 10.0):
        u = urlparse(ws_url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = u.path + (f"?{u.query}" if u.query else "")
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(2.0)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._sock.sendall(req)
        buf = b""
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in buf:
            if time.monotonic() > deadline:
                raise TimeoutError("CDP WebSocket handshake timeout")
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP WebSocket closed during handshake")
            buf += chunk
        header_bytes, self._extra = buf.split(b"\r\n\r\n", 1)
        if b"101" not in header_bytes.split(b"\r\n", 1)[0]:
            raise ConnectionError("CDP WebSocket upgrade rejected")

        self._on_event = on_event
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._id = 0
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self.closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="openx-cdp-read")
        self._reader.start()

    def _recv_exact(self, n: int) -> bytes:
        out = bytearray()
        if self._extra:
            take = min(n, len(self._extra))
            out.extend(self._extra[:take])
            self._extra = self._extra[take:]
        while len(out) < n:
            if self.closed:
                raise ConnectionError("closed")
            try:
                chunk = self._sock.recv(n - len(out))
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("eof")
            out.extend(chunk)
        return bytes(out)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        with self._send_lock:
            self._sock.sendall(bytes(header) + masked)

    def _read_loop(self) -> None:
        try:
            frag = bytearray()
            while not self.closed:
                b1, b2 = self._recv_exact(2)
                fin = bool(b1 & 0x80)
                opcode = b1 & 0x0F
                masked = bool(b2 & 0x80)
                length = b2 & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._recv_exact(8))[0]
                mask_key = self._recv_exact(4) if masked else b""
                data = self._recv_exact(length) if length else b""
                if masked:
                    data = bytes(b ^ mask_key[i & 3] for i, b in enumerate(data))
                if opcode == 0x8:  # close
                    break
                if opcode == 0x9:  # ping
                    try:
                        self._send_frame(0xA, data)
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                if opcode in (0x1, 0x0):
                    frag.extend(data)
                    if fin:
                        raw_text = frag.decode("utf-8", errors="replace")
                        frag.clear()
                        self._handle_message(raw_text)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.closed = True
            with self._lock:
                for ev, box in self._pending.values():
                    box.append({"closed": True})
                    ev.set()
                self._pending.clear()

    def _handle_message(self, raw_text: str) -> None:
        try:
            msg = json.loads(raw_text)
        except ValueError:
            return
        mid = msg.get("id")
        if mid is not None:
            with self._lock:
                slot = self._pending.pop(mid, None)
            if slot is not None:
                ev, box = slot
                box.append(msg)
                ev.set()
            return
        if self._on_event is not None:
            try:
                self._on_event(msg)
            except Exception:  # noqa: BLE001
                pass

    def send(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        if self.closed:
            return {"closed": True}
        ev = threading.Event()
        box: list = []
        with self._lock:
            self._id += 1
            mid = self._id
            self._pending[mid] = (ev, box)
        try:
            payload = json.dumps({"id": mid, "method": method, "params": params or {}}).encode("utf-8")
            self._send_frame(0x1, payload)
        except Exception:  # noqa: BLE001
            with self._lock:
                self._pending.pop(mid, None)
            return {"closed": True}
        if not ev.wait(timeout):
            with self._lock:
                self._pending.pop(mid, None)
            return {"timeout": True}
        return box[0] if box else {"timeout": True}

    def close(self) -> None:
        self.closed = True
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001
            pass


_AUTH_PAGE_STATE_JS = r"""(async () => {
  const t = document.body ? document.body.innerText : '';
  const head = t.slice(0, 3000);
  const errorPage = location.protocol === 'chrome-error:' || location.host === 'chromewebdata';
  const netError = errorPage ? ((t.match(/(?:^|[^A-Z_])((?:ERR|DNS_PROBE)_[A-Z_]+)/) || [])[1] || '') : '';
  const hasPassword = !!document.querySelector('input[type=password]');
  const hasQr = /扫码登录|二维码/.test(head);
  let hasToken = false;
  try { hasToken = !!(localStorage.getItem('token') || sessionStorage.getItem('token')); } catch (_) {}
  const hasLogout = /(?:^|\s|，|,)退出(?:\s|$)/.test(head.slice(0, 500));
  const hasReleases = t.includes('二进制下载') || t.includes('发布时间');
  let authOk = false;
  if (!errorPage && document.readyState === 'complete') {
    if (hasToken || hasLogout) {
      authOk = true;
    } else {
      try {
        const res = await fetch('/openxApi/index/admin/info', { credentials: 'include', cache: 'no-store' });
        if (res && res.ok) {
          const body = await res.json().catch(() => null);
          if (body && (String(body.code) === '0' || body.code === 200) && body.data) authOk = true;
        }
      } catch (_) {}
    }
  }
  return {
    host: location.host, pathname: location.pathname, ready: document.readyState,
    errorPage, netError, hasPassword, hasQr, hasToken, hasLogout, authOk, hasReleases,
  };
})()"""

_CLICK_LOGIN_JS = r"""(() => {
  for (const el of document.querySelectorAll('a, button, span, div')) {
    const r = el.getBoundingClientRect();
    if (!r || r.width < 16 || r.height < 12 || r.top > 120) continue;
    const text = String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (text === '登录' || text === '立即登录') { el.click(); return true; }
  }
  return false;
})()"""

_CLICK_NEXT_JS = r"""(() => {
  for (const s of ['.btn-next', '[aria-label*="next" i]', '[title*="下一页"]', '.el-pagination .btn-next', '.ant-pagination-next']) {
    const el = document.querySelector(s);
    if (el && !el.disabled && el.getAttribute('aria-disabled') !== 'true' && !/disabled/i.test(String(el.className || ''))) {
      el.scrollIntoView({ block: 'center' }); el.click(); return true;
    }
  }
  for (const el of document.querySelectorAll('button, a, li')) {
    const t = String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if ((t === '下一页' || t === '>') && !el.disabled && el.getAttribute('aria-disabled') !== 'true') {
      el.scrollIntoView({ block: 'center' }); el.click(); return true;
    }
  }
  return false;
})()"""


def build_locate_js(release_version: str, file_name: str = "", package_tag: str = "") -> str:
    cfg = json.dumps({
        "releaseVersion": str(release_version or "").strip(),
        "file": str(file_name or "").strip(),
        "packageTag": str(package_tag or "").strip(),
    }, ensure_ascii=False)
    return f"""(() => {{
  const cfg = {cfg};
  const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4;
  }};
  const centerOf = el => {{
    el.scrollIntoView({{ block: 'center', inline: 'nearest' }});
    const r = el.getBoundingClientRect();
    return {{ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }};
  }};
  const findNextBtn = () => {{
    for (const s of ['.btn-next', '[aria-label*="next" i]', '[title*="下一页"]', '.el-pagination .btn-next', '.ant-pagination-next']) {{
      const el = document.querySelector(s);
      if (el && visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true' && !/disabled/i.test(String(el.className || ''))) return el;
    }}
    for (const el of document.querySelectorAll('button, a, li')) {{
      if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
      const t = norm(el.innerText || el.textContent);
      if (t === '下一页' || t === '>') return el;
    }}
    return null;
  }};
  const rv = norm(cfg.releaseVersion).toLowerCase();
  const relScore = text => {{
    const t = norm(text);
    if (!t || t.length > 90) return 0;
    const tl = t.toLowerCase();
    if (rv && tl === rv) return 3;
    if (rv && tl.startsWith(rv + ' ')) return 2;
    return 0;
  }};
  const headings = [];
  for (const el of document.querySelectorAll('h1, h2, h3, h4, h5, span, div, p, a, strong, b, li')) {{
    if (!visible(el)) continue;
    const sc = relScore(el.innerText || el.textContent);
    if (!sc) continue;
    if (Array.from(el.children || []).some(ch => relScore(ch.innerText || ch.textContent) >= sc)) continue;
    headings.push({{ el, score: sc, len: norm(el.innerText || el.textContent).length }});
  }}
  headings.sort((a, b) => b.score - a.score || a.len - b.len);
  if (!headings.length) return {{ found: false, reason: 'release-not-found', hasNext: !!findNextBtn() }};

  let card = null;
  for (const h of headings) {{
    let cur = h.el.parentElement;
    for (let depth = 0; cur && cur !== document.body && depth < 12; depth += 1) {{
      const txt = String(cur.innerText || '');
      if (txt.includes('二进制下载') || cur.querySelector('a[href*="/openx-file/"], a[download]')) {{
        const cnt = (txt.match(/发布时间/g) || []).length;
        if (cnt <= 1) {{ card = cur; break; }}
        if (!card && cnt <= 2) card = cur;
      }}
      cur = cur.parentElement;
    }}
    if (card) break;
  }}
  if (!card) card = headings[0].el.parentElement || document.body;

  const wantTag = norm(cfg.packageTag).toLowerCase();
  const wantFile = norm(cfg.file).toLowerCase();
  const binHeader = Array.from(card.querySelectorAll('span, div, p, button, a, h4, h5'))
    .find(el => visible(el) && norm(el.innerText || el.textContent) === '二进制下载');

  const scoreItem = el => {{
    const t = norm(el.innerText || el.textContent);
    if (!t || t.length > 260 || t === '二进制下载' || t === '修改' || t === '删除') return 0;
    const tl = t.toLowerCase();
    const href = String(el.getAttribute('href') || '').toLowerCase();
    const title = norm(el.getAttribute('title') || '').toLowerCase();
    if (wantTag && (tl === wantTag || title === wantTag)) return 100;
    if (wantFile && (tl.includes(wantFile) || href.includes(wantFile) || title.includes(wantFile))) return 80;
    if (binHeader && (binHeader.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) {{
      if (/\\.(?:gz|exe|zip|json)/i.test(t) || /\\/openx-file\\//i.test(href) || /安装版|组件|基础版/.test(t) || /^[0-9a-f]{{16,}}/i.test(t)) return 50;
    }}
    return 0;
  }};
  const items = [];
  for (const el of card.querySelectorAll('a, button, span, div, li, p')) {{
    if (!visible(el) || el === binHeader) continue;
    const sc = scoreItem(el);
    if (!sc) continue;
    if (Array.from(el.children || []).some(ch => visible(ch) && scoreItem(ch) >= sc)) continue;
    items.push({{ el, score: sc, interactive: el.matches('a, button, [role="button"]') ? 1 : 0, len: norm(el.innerText || el.textContent).length }});
  }}
  if (!items.length && binHeader) {{
    return {{ found: false, needsExpand: true, expandPoint: centerOf(binHeader) }};
  }}
  if (!items.length) return {{ found: false, reason: 'package-not-found', hasNext: false }};

  items.sort((a, b) => b.score - a.score || b.interactive - a.interactive || a.len - b.len);
  const chosen = items[0];
  const primaryEl = chosen.el.closest('a, button, [role="button"]') || chosen.el;
  const primaryPoint = centerOf(primaryEl);
  const altPoints = [];
  const seen = new Set([primaryPoint.x + ',' + primaryPoint.y]);
  const collect = box => {{
    if (!box || box === card) return;
    for (const c of box.querySelectorAll('a, button, [role="button"], svg, i, [class*="download"], [class*="icon"]')) {{
      if (!visible(c) || c === primaryEl) continue;
      const pt = centerOf(c);
      const k = pt.x + ',' + pt.y;
      if (seen.has(k) || Math.hypot(pt.x - primaryPoint.x, pt.y - primaryPoint.y) < 8) continue;
      seen.add(k); altPoints.push(pt);
      if (altPoints.length >= 2) break;
    }}
  }};
  const row = chosen.el.closest('li, tr, [class*="item"], [class*="row"], [class*="file"]') || chosen.el.parentElement;
  collect(row);
  if (!altPoints.length && row && row.parentElement && row.parentElement !== card && norm(row.parentElement.innerText || '').length < 320) collect(row.parentElement);
  return {{ found: true, score: chosen.score, matchedText: norm(chosen.el.innerText || chosen.el.textContent).slice(0, 100), point: primaryPoint, altPoints }};
}})()"""


def _click_point(cdp: _CdpClient, pt: dict) -> None:
    x, y = pt["x"], pt["y"]
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": x, "y": y})
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": x, "y": y})


def _http_json(url: str, timeout: float = 3.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def default_run_session(target: dict, *, timeout_sec: float = DEFAULT_TIMEOUT_SEC,
                        login_grace_sec: float = DEFAULT_LOGIN_GRACE_SEC,
                        env: dict[str, str] | None = None) -> dict[str, Any]:
    """真实无头 Edge 会话：登录内源下载页 → 定位版本与附件 → 点击触发记录 → 取消大文件并回收。"""
    started = time.monotonic()
    page_url = str(target.get("page_url") or "").strip()
    if not page_url:
        return {"ok": False, "transient": False, "reason": "无可用内源下载页地址", "took_ms": 0}
    target_host = (urlparse(page_url).hostname or "").lower()
    if not target_host:
        return {"ok": False, "transient": False, "reason": "内源下载页地址无效", "took_ms": 0}
    edge_exe = find_edge(env)
    if not edge_exe:
        return {"ok": False, "transient": False, "reason": "本机没有找到 Edge", "took_ms": 0}

    sweep_stale_temp_dirs()
    tmp_dir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    dl_dir = tmp_dir / "dl"
    dl_dir.mkdir(parents=True, exist_ok=True)
    proc: subprocess.Popen | None = None
    cdp: _CdpClient | None = None
    browser_pid = 0
    observed_api: set[str] = set()
    observed_req: set[str] = set()
    inflight_api: set[str] = set()
    dl_events: list[dict] = []
    click_at = 0.0
    post_click_hit = False
    target_file = str(target.get("file") or "")

    def on_event(msg: dict) -> None:
        nonlocal post_click_hit
        method = msg.get("method")
        p = msg.get("params") or {}
        if method in ("Browser.downloadWillBegin", "Page.downloadWillBegin"):
            dl_events.append({"guid": p.get("guid") or "", "url": p.get("url") or ""})
            if click_at:
                post_click_hit = True
        elif method == "Target.targetCreated":
            u = (p.get("targetInfo") or {}).get("url") or ""
            if click_at and is_download_like_url(u, target_file):
                post_click_hit = True
        elif method == "Network.requestWillBeSent":
            req = p.get("request") or {}
            u = req.get("url") or ""
            if click_at:
                ap = sanitize_api_path(u)
                if ap:
                    verb = str(req.get("method") or "GET").upper()
                    observed_api.add(ap)
                    observed_req.add(f"{verb} {ap}")
                    rid = p.get("requestId")
                    if rid:
                        inflight_api.add(rid)
                if is_download_like_url(u, target_file):
                    post_click_hit = True
        elif method == "Network.responseReceived":
            headers = (p.get("response") or {}).get("headers") or {}
            disp = str(headers.get("content-disposition") or headers.get("Content-Disposition") or "")
            if click_at and "attachment" in disp.lower():
                post_click_hit = True
        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            rid = p.get("requestId")
            if rid:
                inflight_api.discard(rid)

    try:
        copy_edge_login(tmp_dir, env)
        proc = subprocess.Popen(
            [
                edge_exe,
                "--headless=new",
                "--remote-debugging-port=0",
                f"--user-data-dir={tmp_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-mode",
                "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
        browser_pid = proc.pid or 0
        with _LOCK:
            if browser_pid:
                _RUNNING_PIDS.add(browser_pid)

        # 等 DevToolsActivePort 就绪
        port = 0
        port_deadline = time.monotonic() + START_WAIT_SEC
        port_file = tmp_dir / "DevToolsActivePort"
        while time.monotonic() < port_deadline:
            if port_file.is_file():
                try:
                    raw_port = int(port_file.read_text(encoding="utf-8").splitlines()[0].strip())
                    if raw_port > 0:
                        _http_json(f"http://127.0.0.1:{raw_port}/json/version", timeout=2.0)
                        port = raw_port
                        break
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.3)
        if not port:
            return {"ok": False, "transient": True, "reason": "无头 Edge 没有启动", "took_ms": int((time.monotonic() - started) * 1000)}

        deadline = time.monotonic() + timeout_sec
        ws_url = ""
        for _ in range(20):
            try:
                pages = _http_json(f"http://127.0.0.1:{port}/json/list", timeout=2.0)
                page = next((x for x in pages if isinstance(x, dict) and x.get("type") == "page"), None)
                if page and page.get("webSocketDebuggerUrl"):
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
        if not ws_url:
            return {"ok": False, "transient": True, "reason": "未获取到无头 Edge 页面", "took_ms": int((time.monotonic() - started) * 1000)}

        cdp = _CdpClient(ws_url, on_event=on_event)
        cdp.send("Page.enable")
        cdp.send("Network.enable")
        cdp.send("Target.setDiscoverTargets", {"discover": True})
        beh = cdp.send("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": str(dl_dir), "eventsEnabled": True})
        if beh.get("error"):
            cdp.send("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(dl_dir)})

        cdp.send("Page.navigate", {"url": page_url})
        ready = False
        login_since = 0.0
        anon_since = 0.0
        clicked_login = False
        error_since = 0.0
        renav = 0
        fail_reason = ""

        while time.monotonic() < deadline and not ready:
            time.sleep(0.7)
            if cdp.closed:
                fail_reason = "和无头 Edge 的连接断了"
                break
            ev = cdp.send("Runtime.evaluate", {"expression": _AUTH_PAGE_STATE_JS, "returnByValue": True, "awaitPromise": True})
            st = (((ev.get("result") or {}).get("result") or {}).get("value")) or {}
            if not isinstance(st, dict):
                continue
            if st.get("errorPage"):
                error_since = error_since or time.monotonic()
                if time.monotonic() - error_since < ERROR_PAGE_SEC:
                    continue
                if renav < 1:
                    renav += 1
                    error_since = 0.0
                    cdp.send("Page.navigate", {"url": page_url})
                    continue
                fail_reason = f"页面没有打开（{st.get('netError') or '网络错误'}）"
                break
            error_since = 0.0
            on_target = str(st.get("host") or "").lower().split(":")[0] == target_host
            looks_login = bool(st.get("hasPassword") or (st.get("hasQr") and not on_target))
            if looks_login:
                login_since = login_since or time.monotonic()
                if time.monotonic() - login_since >= login_grace_sec:
                    fail_reason = "需要登录"
                    break
                continue
            login_since = 0.0
            if not on_target or st.get("ready") != "complete":
                continue
            if not st.get("authOk"):
                anon_since = anon_since or time.monotonic()
                if not clicked_login and time.monotonic() - anon_since >= 4.0:
                    clicked_login = True
                    cdp.send("Runtime.evaluate", {"expression": _CLICK_LOGIN_JS, "returnByValue": True})
                    continue
                if time.monotonic() - anon_since >= login_grace_sec:
                    fail_reason = "内源下载页未完成实名登录"
                    break
                continue
            if "/download" not in str(st.get("pathname") or "").lower():
                cdp.send("Page.navigate", {"url": page_url})
                time.sleep(1.0)
                continue
            if not st.get("hasReleases"):
                continue
            ready = True

        if not ready:
            return {"ok": False, "transient": True, "reason": fail_reason or "内源下载页打开超时", "took_ms": int((time.monotonic() - started) * 1000)}

        located = None
        locate_js = build_locate_js(target.get("release_version", ""), target_file, target.get("package_tag", ""))
        for page_no in range(1, MAX_PAGES + 1):
            if time.monotonic() >= deadline:
                break
            loc = (((cdp.send("Runtime.evaluate", {"expression": locate_js, "returnByValue": True}).get("result") or {}).get("result") or {}).get("value"))
            if isinstance(loc, dict) and loc.get("needsExpand") and loc.get("expandPoint"):
                _click_point(cdp, loc["expandPoint"])
                time.sleep(1.2)
                loc = (((cdp.send("Runtime.evaluate", {"expression": locate_js, "returnByValue": True}).get("result") or {}).get("result") or {}).get("value"))
            if isinstance(loc, dict) and loc.get("found"):
                located = loc
                break
            if not isinstance(loc, dict) or not loc.get("hasNext") or page_no >= MAX_PAGES:
                break
            moved = (((cdp.send("Runtime.evaluate", {"expression": _CLICK_NEXT_JS, "returnByValue": True}).get("result") or {}).get("result") or {}).get("value"))
            if not moved:
                break
            time.sleep(1.8)

        if not located or not located.get("point"):
            return {"ok": False, "transient": False, "reason": "内源下载页未找到目标版本或附件", "took_ms": int((time.monotonic() - started) * 1000)}

        points = [located["point"]] + list(located.get("altPoints") or [])
        for pt in points:
            click_at = time.monotonic()
            _click_point(cdp, pt)
            wait_until = min(deadline, time.monotonic() + 3.5)
            while time.monotonic() < wait_until and not post_click_hit and not dl_events:
                if cdp.closed:
                    break
                time.sleep(0.2)
            if post_click_hit or dl_events:
                break

        if not post_click_hit and not dl_events:
            return {"ok": False, "transient": True, "reason": "点击附件后未观测到下载请求", "took_ms": int((time.monotonic() - started) * 1000)}

        settle_min = time.monotonic() + 1.2
        settle_max = min(deadline, time.monotonic() + 2.5)
        while time.monotonic() < settle_max:
            if time.monotonic() >= settle_min and not inflight_api:
                break
            time.sleep(0.15)
        for ev in dl_events:
            if ev.get("guid"):
                cdp.send("Browser.cancelDownload", {"guid": ev["guid"]}, timeout=2.0)

        return {
            "ok": True,
            "method": "dom-click",
            "matched_text": located.get("matchedText") or "",
            "api_paths": sorted(observed_api)[:12],
            "api_requests": sorted(observed_req)[:12],
            "took_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "transient": True, "reason": str(exc)[:160], "took_ms": int((time.monotonic() - started) * 1000)}
    finally:
        if cdp is not None:
            try:
                cdp.send("Browser.close", timeout=1.5)
            except Exception:  # noqa: BLE001
                pass
            cdp.close()
        if browser_pid:
            _kill_pid_tree(browser_pid)
            with _LOCK:
                _RUNNING_PIDS.discard(browser_pid)
        for _ in range(5):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if not tmp_dir.exists():
                break
            time.sleep(0.25)


class OpenxDownloadRecorder:
    """线程安全、持久化去重与断续补记的 PPT Doctor 内源下载记录器。"""

    def __init__(self, *, data_dir: Path | None = None, run_session: Callable | None = None,
                 clock: Callable[[], float] = time.time, frozen: bool | None = None):
        self._data_dir = data_dir
        self._injected_runner = run_session is not None
        self._run_session = run_session or default_run_session
        self._clock = clock
        self._frozen = frozen
        self._lock = threading.Lock()
        self._exec_lock = threading.Lock()
        self._disposed = False
        self._resume_timer: threading.Timer | None = None

    def _state_file(self) -> Path:
        if self._data_dir is not None:
            d = Path(self._data_dir)
        else:
            from .config import data_dir
            d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d / RECORD_FILE

    def _empty(self) -> dict[str, Any]:
        return {
            "version": 1,
            "recorded": {},
            "pending_queue": [],
            "stats": {
                "attempted": 0, "succeeded": 0, "failed": 0,
                "last_status": "", "last_reason": "", "last_release": "",
                "last_at": "", "last_took_ms": 0, "last_api_paths": [], "last_api_requests": [],
            },
        }

    def _load(self) -> dict[str, Any]:
        p = self._state_file()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return self._empty()
            base = self._empty()
            base["recorded"] = dict(raw.get("recorded") or {})
            base["pending_queue"] = [x for x in (raw.get("pending_queue") or []) if isinstance(x, dict)][:MAX_PENDING_QUEUE]
            base["stats"].update(raw.get("stats") or {})
            return base
        except Exception:  # noqa: BLE001
            return self._empty()

    def _save(self, store: dict[str, Any]) -> None:
        try:
            p = self._state_file()
            rec = store.get("recorded") or {}
            if len(rec) > MAX_RECORDED:
                ordered = sorted(rec.items(), key=lambda kv: str((kv[1] or {}).get("at") or ""), reverse=True)
                store["recorded"] = dict(ordered[:MAX_RECORDED])
            tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}-{int(self._clock() * 1000)}")
            tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                tmp.replace(p)
            except OSError:
                p.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def enabled(self) -> bool:
        if os.environ.get("PPTX_FINDER_OPENX_RECORD", "").strip() == "0":
            return False
        return self._injected_runner or is_active(self._frozen)

    def _key(self, release_version: str) -> str:
        return str(release_version or "").strip().lower()

    def _upsert_pending(self, store: dict, entry: dict) -> None:
        k = self._key(entry.get("release_version", ""))
        q = [x for x in store.get("pending_queue") or [] if self._key(x.get("release_version", "")) != k]
        q.append(entry)
        store["pending_queue"] = q[-MAX_PENDING_QUEUE:]

    def _remove_pending(self, store: dict, key: str) -> None:
        store["pending_queue"] = [x for x in store.get("pending_queue") or [] if self._key(x.get("release_version", "")) != key]

    def _execute(self, item: dict, *, from_pending: bool = False) -> dict[str, Any]:
        key = self._key(item.get("release_version", ""))
        if not key or not self.enabled() or self._disposed:
            return {"status": "skipped", "release_version": item.get("release_version", "")}
        with self._exec_lock:
            with self._lock:
                store = self._load()
                if key in store["recorded"]:
                    self._remove_pending(store, key)
                    self._save(store)
                    return {"status": "recorded", "cached": True, "release_version": item["release_version"]}
                attempts = int(item.get("attempts") or 0) if from_pending else 0
                pending_item = {**item, "queued_at": item.get("queued_at") or self._clock(), "attempts": attempts}
                self._upsert_pending(store, pending_item)
                self._save(store)

            try:
                res = self._run_session(pending_item)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "transient": True, "reason": str(exc)[:160], "took_ms": 0}

            if self._disposed:
                return {"status": "skipped", "reason": "disposed", "release_version": item["release_version"]}

            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))
            with self._lock:
                store = self._load()
                st = store["stats"]
                st["attempted"] = int(st.get("attempted") or 0) + 1
                st["last_release"] = item["release_version"]
                st["last_at"] = now_iso
                st["last_took_ms"] = int((res or {}).get("took_ms") or 0)
                if res and res.get("api_paths"):
                    st["last_api_paths"] = list(res["api_paths"])[:12]
                if res and res.get("api_requests"):
                    st["last_api_requests"] = list(res["api_requests"])[:12]
                if res and res.get("ok"):
                    store["recorded"][key] = {
                        "at": now_iso,
                        "release_version": item["release_version"],
                        "file": item.get("file") or "",
                        "kind": item.get("kind") or "update",
                        "method": res.get("method") or "dom-click",
                        "api_paths": list(res.get("api_paths") or [])[:12],
                        "api_requests": list(res.get("api_requests") or [])[:12],
                    }
                    self._remove_pending(store, key)
                    st["succeeded"] = int(st.get("succeeded") or 0) + 1
                    st["last_status"] = "recorded"
                    st["last_reason"] = ""
                    self._save(store)
                    log.info("内源下载记录已同步：%s（%dms）", item["release_version"], st["last_took_ms"])
                    return {"status": "recorded", "release_version": item["release_version"]}

                reason = str((res or {}).get("reason") or "后台记录未完成")
                st["failed"] = int(st.get("failed") or 0) + 1
                st["last_status"] = "failed"
                st["last_reason"] = reason
                next_attempts = attempts + 1
                if (res or {}).get("transient", True) and next_attempts < MAX_PENDING_ATTEMPTS:
                    self._upsert_pending(store, {**pending_item, "attempts": next_attempts})
                else:
                    self._remove_pending(store, key)
                self._save(store)
                log.info("内源下载后台记录未完成（不影响本次更新）%s：%s", item["release_version"], reason)
                return {"status": "failed", "reason": reason, "release_version": item["release_version"]}

    def record(self, *, release_version: str, file_name: str = "", package_tag: str = "",
               page_url: str = "", kind: str = "update", background: bool = True) -> dict[str, Any] | threading.Thread | None:
        """在下载校验成功后记录内源下载量。background=True 时立刻落盘待补记项并在守护线程执行。"""
        if not self.enabled() or self._disposed:
            return None
        rv = str(release_version or "").strip()
        key = self._key(rv)
        if not key:
            return None
        from . import openx_source
        url = str(page_url or openx_source.PAGE_URL).strip()
        item = {
            "release_version": rv,
            "file": str(file_name or "").strip(),
            "package_tag": str(package_tag or "").strip(),
            "page_url": url,
            "kind": str(kind or "update").strip(),
            "queued_at": self._clock(),
            "attempts": 0,
        }
        with self._lock:
            store = self._load()
            if key in store["recorded"]:
                return {"status": "recorded", "cached": True, "release_version": rv}
            self._upsert_pending(store, item)
            self._save(store)
        if not background:
            return self._execute(item, from_pending=False)
        th = threading.Thread(target=self._execute, args=(item,), kwargs={"from_pending": False},
                              daemon=True, name=f"openx-rec-{rv[:24]}")
        th.start()
        return th

    def resume_pending(self, *, delay_sec: float = DEFAULT_RESUME_DELAY_SEC, background: bool = True) -> list[dict] | threading.Timer | None:
        """启动补记：上次更新后立即重启打断了后台记录时，延迟补记一次。"""
        if not self.enabled() or self._disposed:
            return None
        now_ts = self._clock()
        with self._lock:
            store = self._load()
            valid = []
            for p in store.get("pending_queue") or []:
                k = self._key(p.get("release_version", ""))
                if not k or k in store["recorded"]:
                    continue
                if int(p.get("attempts") or 0) >= MAX_PENDING_ATTEMPTS:
                    continue
                if now_ts - float(p.get("queued_at") or 0) > PENDING_MAX_AGE_SEC:
                    continue
                valid.append(p)
            if len(valid) != len(store.get("pending_queue") or []):
                store["pending_queue"] = valid
                self._save(store)
        if not valid:
            return None

        def _drain() -> list[dict]:
            out = []
            for cur in list(valid):
                if self._disposed or not self.enabled():
                    break
                out.append(self._execute(cur, from_pending=True))
            return out

        if not background:
            return _drain()
        if self._resume_timer is not None:
            try:
                self._resume_timer.cancel()
            except Exception:  # noqa: BLE001
                pass
        timer = threading.Timer(max(0.0, float(delay_sec)), _drain)
        timer.daemon = True
        timer.start()
        self._resume_timer = timer
        return timer

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            store = self._load()
        return {
            "enabled": self.enabled(),
            "recorded_count": len(store.get("recorded") or {}),
            "pending_count": len(store.get("pending_queue") or []),
            "stats": dict(store.get("stats") or {}),
        }

    def dispose(self) -> None:
        self._disposed = True
        if self._resume_timer is not None:
            try:
                self._resume_timer.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._resume_timer = None
        with _LOCK:
            pids = list(_RUNNING_PIDS)
            _RUNNING_PIDS.clear()
        for pid in pids:
            _kill_pid_tree(pid)


RECORDER = OpenxDownloadRecorder()


def record_download_async(*, release_version: str, file_name: str = "", package_tag: str = "",
                          page_url: str = "", kind: str = "update") -> None:
    """安全入口：任何异常全部吞掉，绝不影响主流程下载。"""
    try:
        RECORDER.record(
            release_version=release_version,
            file_name=file_name,
            package_tag=package_tag,
            page_url=page_url,
            kind=kind,
            background=True,
        )
    except Exception:  # noqa: BLE001
        pass


def schedule_resume_pending(delay_sec: float = DEFAULT_RESUME_DELAY_SEC) -> None:
    try:
        RECORDER.resume_pending(delay_sec=delay_sec, background=True)
    except Exception:  # noqa: BLE001
        pass
