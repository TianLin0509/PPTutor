"""Single-owner, file-spooled Hub service. Work runs on the main thread.

The operating-system lock, not a PID file, owns a spool. An interrupted write
is reported to its caller and is never replayed during recovery.
"""
from __future__ import annotations

import errno
from functools import partial
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

from . import __version__, hub_cli

READ_ACTIONS = frozenset(("status", "search", "read", "catalog", "report"))
JOB_NAME = re.compile(r"[0-9a-f]{32}\.json\Z")
DIRECTORIES = ("requests", "working", "responses", "progress")
FILES = ("server.lock", "server.json", "stop.json", "server-error.log")


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _real_path(path):
    # Reject junctions too: resolving first would hide that the caller supplied
    # a link. No queue entry may redirect reads/deletes into another directory.
    for parent in (path, *path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise hub_cli.HubError("INVALID_SPOOL", f"常驻队列不能使用链接目录或文件：{path}")
    return path


def _spool_path(value):
    path = Path(value)
    if not path.is_absolute() or path.parent == path or str(path).startswith("\\\\"):
        raise hub_cli.HubError("INVALID_SPOOL", "spool 必须为专用本地绝对目录")
    _real_path(path)
    path = path.resolve()
    if path.exists():
        for entry in path.iterdir():
            if entry.name not in (*DIRECTORIES, *FILES) and not re.fullmatch(
                r"(?:server|stop)\.json\.(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.tmp", entry.name
            ):
                raise hub_cli.HubError("INVALID_SPOOL", "spool 中有非队列文件，请使用专用目录")
            _real_path(entry)
    else:
        path.mkdir(parents=True)
    return path


class SpoolLock:
    """Lock one permanent byte; never unlink a live or released lock file."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        _real_path(self.path)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        if self.path.stat().st_size == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        return True

    def __exit__(self, *_):
        if self.handle is not None:
            # close releases the kernel lock even on an exception or crash.
            self.handle.close()
            self.handle = None


def _request(path):
    _real_path(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        return {}
    try:
        value = json.loads(hub_cli.retry_file_change(path.read_text, "utf-8-sig"))
    except (ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _action(request):
    value = request.get("action", "")
    return value if isinstance(value, str) else ""


def _complete(path, request):
    if not path.exists():
        return False
    # A response can legitimately exceed the 8 MB request limit.
    _real_path(path)
    try:
        value = json.loads(hub_cli.retry_file_change(path.read_text, "utf-8"))
    except (ValueError, UnicodeError):
        return False
    return (isinstance(value, dict) and isinstance(value.get("ok"), bool)
            and value.get("apiVersion") == hub_cli.API_VERSION
            and value.get("requestId") == request.get("requestId", "")
            and value.get("action") == _action(request)
            and isinstance(value.get("doctorVersion"), str)
            and isinstance(value.get("elapsedMs"), (int, float))
            and (value["ok"] or isinstance(value.get("error"), dict)))


def _jobs(directory):
    for path in sorted(directory.glob("*.json")):
        if not JOB_NAME.fullmatch(path.name):
            raise hub_cli.HubError("INVALID_JOB_ID", f"队列文件名不是32位小写jobId：{path.name}")
        _real_path(path)
        if not path.is_file():
            raise hub_cli.HubError("INVALID_REQUEST", "队列任务必须是文件")
        yield path


def _recover(spool):
    for path in _jobs(spool / "working"):
        request = _request(path)
        response = spool / "responses" / path.name
        if not _complete(response, request):
            action = _action(request)
            read_only = action in READ_ACTIONS
            message = "Doctor 上次退出时任务尚未完成，请检查输出后重试"
            payload = dict(apiVersion=hub_cli.API_VERSION, doctorVersion=__version__,
                           requestId=request.get("requestId", ""), action=action,
                           elapsedMs=0, ok=False, code="REQUEST_INTERRUPTED",
                           error=dict(code="REQUEST_INTERRUPTED", message=message),
                           retryable=read_only, resultMayBePartial=not read_only)
            _real_path(response)
            hub_cli.write_json(response, payload)
            progress = spool / "progress" / path.name
            _real_path(progress)
            hub_cli.write_json(progress, {**payload, "state": "failed", "stage": "interrupted",
                                         "message": message, "done": 0, "total": None})
        hub_cli.retry_file_change(path.unlink)
        staging = path.with_suffix(".result.tmp")
        _real_path(staging)
        if staging.exists():
            hub_cli.retry_file_change(staging.unlink)


class Heartbeat:
    def __init__(self, spool, idle_timeout, interval):
        self.spool = spool
        self.interval = interval
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.failure = None
        self.info = dict(apiVersion=hub_cli.API_VERSION, doctorVersion=__version__,
                         pid=os.getpid(), instanceId=uuid.uuid4().hex,
                         startedAt=_utc(), heartbeatAt=_utc(), state="ready",
                         idleTimeoutSec=idle_timeout)
        self.thread = threading.Thread(target=self._run, name="hub-heartbeat", daemon=True)

    def publish(self, state=None):
        with self.lock:
            if state:
                self.info["state"] = state
            self.info["heartbeatAt"] = _utc()
            target = self.spool / "server.json"
            _real_path(target)
            hub_cli.write_json(target, dict(self.info))

    def _run(self):
        try:
            while not self.stop.wait(self.interval):
                self.publish()
        except Exception as exc:
            self.failure = exc
            self.stop.set()

    def check(self):
        if self.failure:
            raise RuntimeError("常驻心跳写入失败") from self.failure

    def start(self):
        self.publish()
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()
        self.check()
        self.publish("stopping")

    def requested_stop(self):
        path = self.spool / "stop.json"
        if not path.exists():
            return False
        return _request(path).get("instanceId") == self.info["instanceId"]


def serve(spool, *, idle_timeout=600, heartbeat_interval=2, poll_interval=0.15, run_request=None):
    """Internal timing hooks make recovery/idle tests fast; CLI stays at 600 s."""
    spool = _spool_path(spool)
    run_request = run_request or partial(hub_cli.main, allow_server=False)
    with SpoolLock(spool / "server.lock") as owned:
        if not owned:
            return 0
        for directory in DIRECTORIES:
            _real_path(spool / directory).mkdir(exist_ok=True)
        _recover(spool)
        heartbeat = Heartbeat(spool, idle_timeout, heartbeat_interval)
        heartbeat.start()
        idle_since = time.monotonic()
        try:
            while True:
                heartbeat.check()
                if heartbeat.requested_stop() or time.monotonic() - idle_since >= idle_timeout:
                    return 0
                path = next(_jobs(spool / "requests"), None)
                if path is None:
                    heartbeat.stop.wait(poll_interval)
                    continue
                request = _request(path)
                response = spool / "responses" / path.name
                progress = spool / "progress" / path.name
                working = spool / "working" / path.name
                staging = working.with_suffix(".result.tmp")
                for target in (response, progress, working, staging):
                    _real_path(target)
                if response.exists():
                    if not _complete(response, request):
                        raise hub_cli.HubError("JOB_ID_CONFLICT", "jobId 已对应另一结果，不执行重复任务")
                    hub_cli.retry_file_change(path.unlink)
                    continue
                if working.exists():
                    raise hub_cli.HubError("JOB_ID_CONFLICT", "jobId 已在执行，不重复领取")
                # The Hub only publishes fully written immutable request files.
                hub_cli.retry_file_change(path.rename, working)
                heartbeat.publish("busy")
                run_request(["--hub", _action(request), "--in", str(working),
                             "--out", str(staging), "--progress", str(progress)])
                if not _complete(staging, request):
                    raise RuntimeError("任务未生成完整匹配结果，保留在途请求供下次恢复")
                # After publication the Hub may read and delete the response
                # immediately. Validate privately, then never read it again.
                hub_cli.retry_file_change(os.replace, staging, response)
                hub_cli.retry_file_change(working.unlink)
                heartbeat.check()
                idle_since = time.monotonic()
                heartbeat.publish("ready")
        finally:
            heartbeat.close()


def _background_priority():
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x00004000):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.nice(5)


def main(argv):
    spool = None
    try:
        parser = hub_cli._Parser(add_help=False)
        parser.add_argument("--hub", choices=["serve"], required=True)
        parser.add_argument("--spool", required=True)
        args = parser.parse_args(argv)
        spool = _spool_path(args.spool)
        _background_priority()
        return serve(spool)
    except Exception:
        if spool is not None:
            log = _real_path(spool / "server-error.log")
            with log.open("a", encoding="utf-8") as handle:
                handle.write(_utc() + "\n" + traceback.format_exc() + "\n")
        return 4
