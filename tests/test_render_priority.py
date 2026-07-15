"""预览优先门：缩略图渲染让路给预览（共享 COM 锁下预览不被一屏缩略图饿死）。

用假 COM（每次渲染 sleep）验证：先排一批缩略图占锁，途中来一个预览，
预览应插队、不被排在所有缩略图之后。
"""
from __future__ import annotations

import sys
import threading
import time
import types

from pptx_finder import renderer


def test_pid_for_app_accepts_powerpoint_hwnd_as_callable(monkeypatch):
    """Real PowerPoint dynamic dispatch exposes HWND as a bound method."""
    app = types.SimpleNamespace(HWND=lambda: 4321)
    monkeypatch.setitem(
        sys.modules,
        "win32process",
        types.SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (7, 9001)),
    )

    assert renderer._pid_for_app(app) == 9001


class _FakeSlide:
    def Export(self, out, fmt, w, h):
        time.sleep(0.2)                       # 模拟一次 COM 导出耗时
        with open(out, "wb") as f:
            f.write(b"x")


class _FakeSlides:
    def __init__(self, n):
        self._n = n

    @property
    def Count(self):
        return self._n

    def __call__(self, i):
        return _FakeSlide()


class _FakePageSetup:
    SlideWidth = 960.0
    SlideHeight = 540.0


class _FakePres:
    def __init__(self):
        self.Slides = _FakeSlides(5)
        self.PageSetup = _FakePageSetup()

    def Close(self):
        pass


class _FakeApp:
    class _Presentations:
        def Open(self, path, ReadOnly=1, WithWindow=0):
            return _FakePres()

    def __init__(self):
        self.Presentations = _FakeApp._Presentations()
        self.quit_calls = 0

    def Quit(self):
        self.quit_calls += 1


class _BrokenSlide:
    def Export(self, out, fmt, w, h):
        raise RuntimeError("export failed")


class _BrokenSlides(_FakeSlides):
    def __call__(self, i):
        return _BrokenSlide()


class _BrokenPres(_FakePres):
    def __init__(self):
        self.Slides = _BrokenSlides(5)
        self.PageSetup = _FakePageSetup()


def test_preview_preempts_thumbnails(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "_get_app", lambda: _FakeApp())
    src = tmp_path / "x.pptx"
    src.write_bytes(b"dummy")

    done: list[tuple[str, float]] = []
    lk = threading.Lock()

    def record(label):
        with lk:
            done.append((label, time.perf_counter()))

    def thumb(i):
        renderer.render_page(str(src), 1, cache_key=f"t{i}", long_edge=480)   # 低优先
        record(f"thumb{i}")

    def preview():
        renderer.render_page(str(src), 1, cache_key="prev", long_edge=2560, hi_priority=True)
        record("preview")

    # 先排 6 个缩略图占锁，0.1s 后来一个预览
    threads = [threading.Thread(target=thumb, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    pv = threading.Thread(target=preview)
    pv.start()
    for t in threads + [pv]:
        t.join()

    order = [label for label, _ in sorted(done, key=lambda x: x[1])]
    assert "preview" in order
    pv_idx = order.index("preview")
    # 预览不应排在最后（被缩略图饿死）；且至少有 2 个缩略图在它之后完成 = 有效插队
    assert pv_idx < len(order) - 1, f"预览被缩略图饿死，完成顺序={order}"
    assert (len(order) - 1 - pv_idx) >= 2, f"预览未有效插队，完成顺序={order}"
    # 单槽状态归零（每次 finally 都释放）
    assert renderer._hi_waiting == 0
    assert renderer._busy is False
    assert renderer._waiting_by_priority == {}


def test_numeric_priority_orders_waiting_render_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "_get_app", lambda: _FakeApp())
    src = tmp_path / "x.pptx"
    src.write_bytes(b"dummy")

    done: list[tuple[str, float]] = []
    lk = threading.Lock()

    def record(label):
        with lk:
            done.append((label, time.perf_counter()))

    def task(label, priority):
        renderer.render_page(
            str(src),
            1,
            cache_key=label,
            long_edge=480,
            hi_priority=False,
            priority=priority,
        )
        record(label)

    blocker = threading.Thread(target=task, args=("blocker", 100))
    blocker.start()
    time.sleep(0.05)
    prefetch = threading.Thread(target=task, args=("prefetch", 220))
    visible_thumb = threading.Thread(target=task, args=("visible_thumb", 5))
    prefetch.start()
    visible_thumb.start()
    for t in (blocker, prefetch, visible_thumb):
        t.join()

    order = [label for label, _ in sorted(done, key=lambda x: x[1])]
    assert order.index("visible_thumb") < order.index("prefetch")
    assert renderer._waiting_by_priority == {}


def test_failed_render_is_short_circuited_by_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    renderer._failed_until.clear()
    renderer.close_current_presentation()
    src = tmp_path / "broken.pptx"
    src.write_bytes(b"dummy")
    opens = 0

    class BrokenApp:
        class PresentationsImpl:
            def Open(self, path, ReadOnly=1, WithWindow=0):
                nonlocal opens
                opens += 1
                return _BrokenPres()

        def __init__(self):
            self.Presentations = BrokenApp.PresentationsImpl()

    monkeypatch.setattr(renderer, "_get_app", lambda: BrokenApp())

    assert renderer.render_page(str(src), 1, cache_key="broken", long_edge=480) is None
    assert opens == 1
    assert renderer.render_page(str(src), 1, cache_key="broken", long_edge=480) is None
    assert opens == 1
    renderer._failed_until.clear()


def test_shutdown_closes_owned_presentation_but_never_quits_powerpoint(monkeypatch):
    app = _FakeApp()
    pres = _FakePres()
    closed = []
    pres.Close = lambda: closed.append(True)

    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    # No environment override may weaken the user's open-PowerPoint safety boundary.
    monkeypatch.setenv("PPTUTOR_QUIT_POWERPOINT_ON_SHUTDOWN", "1")
    renderer._state.app = app
    renderer._state.pres = pres
    renderer._state.pres_path = "C:/tmp/rendered.pptx"
    renderer._state.pres_key = "k"

    renderer.shutdown()

    assert closed == [True]
    assert app.quit_calls == 0
    assert getattr(renderer._state, "app", None) is None


def test_shutdown_closes_every_renderer_owned_presentation_reference(monkeypatch):
    app = _FakeApp()
    first = _FakePres()
    second = _FakePres()
    user_document = _FakePres()
    closed: list[str] = []
    first.Close = lambda: closed.append("first")
    second.Close = lambda: closed.append("second")
    user_document.Close = lambda: closed.append("user")

    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    renderer._state.app = app
    renderer._state.pres = second
    renderer._state.pres_path = "C:/cache/second.pptx"
    renderer._state.pres_key = "second"
    renderer._state.owned_presentations = [first, second]

    renderer.shutdown()

    assert sorted(closed) == ["first", "second"]
    assert "user" not in closed
    assert getattr(renderer._state, "owned_presentations", []) == []


def test_failed_close_keeps_owned_presentation_for_a_later_retry(tmp_path):
    snapshot = tmp_path / "hash-like-preview.pptx"
    snapshot.write_bytes(b"preview")

    class FlakyPresentation:
        allow_close = False

        def Close(self):
            if not self.allow_close:
                raise RuntimeError("PowerPoint busy")

    pres = FlakyPresentation()
    renderer._state.pres = pres
    renderer._state.pres_path = str(snapshot)
    renderer._state.pres_key = "hash-like-preview"
    renderer._state.snapshot_path = str(snapshot)
    renderer._state.snapshot_src = "C:/user/deck.pptx"
    renderer._state.snapshot_key = "hash-like-preview"
    renderer._state.owned_presentations = [pres]

    assert renderer._close_pres() is False
    assert getattr(renderer._state, "owned_presentations", []) == [pres]
    assert snapshot.exists()
    assert getattr(renderer._state, "snapshot_path", None) == str(snapshot)

    pres.allow_close = True
    assert renderer._close_pres() is True
    assert getattr(renderer._state, "owned_presentations", []) == []
    assert not snapshot.exists()


def test_failed_owned_powerpoint_exit_keeps_reference_and_handle_for_retry(monkeypatch):
    """Do not forget a proven-owned headless process that failed bounded exit."""

    class Handle:
        closed = False

        def Close(self):
            self.closed = True

    app = types.SimpleNamespace(Presentations=types.SimpleNamespace(Count=0))
    handle = Handle()
    monkeypatch.setattr(renderer, "_pid_has_visible_window", lambda _pid: False)
    monkeypatch.setattr(renderer, "_request_owned_powerpoint_exit", lambda *_a, **_k: False)
    renderer._state.app = app
    renderer._state.app_owned_pid = 9004
    renderer._state.app_owned_handle = handle
    renderer._state.owned_presentations = []

    try:
        renderer._release_local_app_reference()

        assert renderer._state.app is app
        assert renderer._state.app_owned_pid == 9004
        assert renderer._state.app_owned_handle is handle
        assert not handle.closed
    finally:
        renderer._state.app = None
        renderer._state.app_owned_pid = None
        renderer._state.app_owned_handle = None
        renderer._state.owned_presentations = []


def test_shutdown_uses_bounded_exit_for_proven_owned_empty_headless_powerpoint(monkeypatch):
    class EmptyPresentations:
        Count = 0

    class OwnedHeadlessApp:
        HWND = 123
        Presentations = EmptyPresentations()

        def __init__(self):
            self.quit_calls = 0

        def Quit(self):
            self.quit_calls += 1
            raise AssertionError("synchronous Application.Quit may block for a minute")

    app = OwnedHeadlessApp()
    exit_requests = []
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    monkeypatch.setattr(renderer, "_pid_for_app", lambda _app: 9001, raising=False)
    monkeypatch.setattr(
        renderer,
        "_pid_has_visible_window",
        lambda _pid: False,
        raising=False,
    )
    monkeypatch.setattr(
        renderer,
        "_request_owned_powerpoint_exit",
        lambda got_app, pid, *, owned_handle=None: exit_requests.append(
            (got_app, pid, owned_handle)
        )
        or True,
        raising=False,
    )
    renderer._state.app = app
    renderer._state.app_owned_pid = 9001
    renderer._state.app_owned_handle = None
    renderer._state.pres = None
    renderer._state.owned_presentations = []

    renderer.shutdown()

    assert app.quit_calls == 0
    assert exit_requests == [(app, 9001, None)]


def test_shutdown_uses_creation_time_process_handle_when_headless_hwnd_is_unavailable(
    monkeypatch,
):
    class EmptyHeadlessApp:
        Presentations = types.SimpleNamespace(Count=0)

        def HWND(self):
            raise RuntimeError("headless PowerPoint has no HWND member yet")

    class OwnedHandle:
        def __init__(self):
            self.closed = False

        def Close(self):
            self.closed = True

    app = EmptyHeadlessApp()
    handle = OwnedHandle()
    exit_requests = []
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    monkeypatch.setattr(renderer, "_pid_for_app", lambda _app: None)
    monkeypatch.setattr(renderer, "_pid_has_visible_window", lambda _pid: False)
    monkeypatch.setattr(
        renderer,
        "_request_owned_powerpoint_exit",
        lambda got_app, pid, *, owned_handle=None: exit_requests.append(
            (got_app, pid, owned_handle)
        )
        or True,
    )
    renderer._state.app = app
    renderer._state.app_owned_pid = 9001
    renderer._state.app_owned_handle = handle
    renderer._state.pres = None
    renderer._state.owned_presentations = []

    renderer.shutdown()

    assert exit_requests == [(app, 9001, handle)]
    assert handle.closed


def test_bounded_owned_exit_terminates_only_after_grace_and_second_visibility_check(
    monkeypatch,
):
    events = []

    class FakeHandle:
        def Close(self):
            events.append("close-handle")

    app = types.SimpleNamespace(
        HWND=123,
        Presentations=types.SimpleNamespace(Count=0),
    )
    waits = iter([258, 258, 0])  # alive, grace timeout, then terminated
    fake_con = types.SimpleNamespace(
        PROCESS_TERMINATE=1,
        SYNCHRONIZE=2,
        WAIT_OBJECT_0=0,
        WM_CLOSE=0x0010,
    )
    monkeypatch.setitem(sys.modules, "win32con", fake_con)
    monkeypatch.setitem(
        sys.modules,
        "win32api",
        types.SimpleNamespace(
            OpenProcess=lambda access, inherit, pid: events.append(
                ("open", access, inherit, pid)
            )
            or FakeHandle(),
            TerminateProcess=lambda _handle, code: events.append(("terminate", code)),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32event",
        types.SimpleNamespace(
            WAIT_OBJECT_0=0,
            WAIT_TIMEOUT=258,
            WaitForSingleObject=lambda _handle, timeout: events.append(("wait", timeout))
            or next(waits),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        types.SimpleNamespace(
            PostMessage=lambda hwnd, msg, wparam, lparam: events.append(
                ("post", hwnd, msg, wparam, lparam)
            )
        ),
    )
    monkeypatch.setattr(renderer, "_pid_for_app", lambda _app: 9001)
    monkeypatch.setattr(renderer, "_pid_has_visible_window", lambda _pid: False)

    assert renderer._request_owned_powerpoint_exit(app, 9001, graceful_wait_sec=0.01)
    assert ("post", 123, 0x0010, 0, 0) in events
    assert ("terminate", 0) in events
    assert events[-1] == "close-handle"


def test_bounded_owned_exit_never_terminates_if_session_becomes_visible(monkeypatch):
    events = []

    class FakeHandle:
        def Close(self):
            events.append("close-handle")

    app = types.SimpleNamespace(
        HWND=123,
        Presentations=types.SimpleNamespace(Count=0),
    )
    visible = iter([False, True])
    fake_con = types.SimpleNamespace(
        PROCESS_TERMINATE=1,
        SYNCHRONIZE=2,
        WAIT_OBJECT_0=0,
        WM_CLOSE=0x0010,
    )
    monkeypatch.setitem(sys.modules, "win32con", fake_con)
    monkeypatch.setitem(
        sys.modules,
        "win32api",
        types.SimpleNamespace(
            OpenProcess=lambda *_args: FakeHandle(),
            TerminateProcess=lambda *_args: events.append("terminate"),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32event",
        types.SimpleNamespace(
            WAIT_OBJECT_0=0,
            WAIT_TIMEOUT=258,
            WaitForSingleObject=lambda *_args: 258,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        types.SimpleNamespace(PostMessage=lambda *_args: events.append("post")),
    )
    monkeypatch.setattr(renderer, "_pid_for_app", lambda _app: 9001)
    monkeypatch.setattr(renderer, "_pid_has_visible_window", lambda _pid: next(visible))

    assert not renderer._request_owned_powerpoint_exit(app, 9001, graceful_wait_sec=0)
    assert "post" in events
    assert "terminate" not in events
    assert events[-1] == "close-handle"


def test_shutdown_never_quits_owned_pid_after_it_becomes_user_visible(monkeypatch):
    class EmptyPresentations:
        Count = 0

    class VisibleApp:
        HWND = 123
        Presentations = EmptyPresentations()

        def __init__(self):
            self.quit_calls = 0

        def Quit(self):
            self.quit_calls += 1

    app = VisibleApp()
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    monkeypatch.setattr(renderer, "_pid_for_app", lambda _app: 9002, raising=False)
    monkeypatch.setattr(
        renderer,
        "_pid_has_visible_window",
        lambda _pid: True,
        raising=False,
    )
    renderer._state.app = app
    renderer._state.app_owned_pid = 9002
    renderer._state.pres = None
    renderer._state.owned_presentations = []

    renderer.shutdown()

    assert app.quit_calls == 0


def test_shutdown_never_quits_process_that_contains_any_user_document(monkeypatch):
    class PresentationsWithUserDocument:
        Count = 1

    class AppWithUserDocument:
        HWND = 123
        Presentations = PresentationsWithUserDocument()

        def __init__(self):
            self.quit_calls = 0

        def Quit(self):
            self.quit_calls += 1

    app = AppWithUserDocument()
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    monkeypatch.setattr(renderer, "_pid_for_app", lambda _app: 9003)
    monkeypatch.setattr(renderer, "_pid_has_visible_window", lambda _pid: False)
    renderer._state.app = app
    renderer._state.app_owned_pid = 9003
    renderer._state.pres = None
    renderer._state.owned_presentations = []

    renderer.shutdown()

    assert app.quit_calls == 0


def test_shutdown_never_uninitializes_a_com_apartment_it_did_not_initialize(monkeypatch):
    app = _FakeApp()
    calls: list[str] = []
    pythoncom = types.SimpleNamespace(CoUninitialize=lambda: calls.append("uninit"))
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    renderer._state.app = app
    renderer._state.app_owned_pid = None
    renderer._state.com_initialized_by_renderer = False
    renderer._state.pres = None
    renderer._state.owned_presentations = []

    renderer.shutdown()

    assert calls == []


def test_owned_powerpoint_pid_falls_back_to_new_process_diff(monkeypatch):
    app = object()
    observations = iter([set(), {4242}])
    monkeypatch.setattr(renderer, "_pid_for_app", lambda _app: None)
    monkeypatch.setattr(
        renderer,
        "_powerpoint_process_ids",
        lambda: next(observations),
    )

    pid = renderer._discover_owned_powerpoint_pid(
        app,
        existing_pids=set(),
        timeout_sec=0.1,
    )

    assert pid == 4242


def test_render_page_snapshot_opens_temp_copy_not_live_file(tmp_path, monkeypatch):
    renderer.shutdown()
    renderer._failed_until.clear()
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    src = tmp_path / "live-editing.pptx"
    src.write_bytes(b"dummy")
    opened: list[str] = []

    class RecordingApp:
        class PresentationsImpl:
            def Open(self, path, ReadOnly=1, WithWindow=0):
                opened.append(path)
                return _FakePres()

        def __init__(self):
            self.Presentations = RecordingApp.PresentationsImpl()

    monkeypatch.setattr(renderer, "_get_app", lambda: RecordingApp())

    out = renderer.render_page(
        str(src),
        1,
        cache_key="snapshot-test",
        long_edge=480,
        use_snapshot=True,
    )

    assert out == tmp_path / "snapshot-test_1_480.png"
    assert opened
    assert opened[0] != str(src)
    assert str(tmp_path / "render_snapshots") in opened[0]
    assert (tmp_path / "render_snapshots" / "snapshot-test.pptx").exists()
    renderer.shutdown()
    assert not (tmp_path / "render_snapshots" / "snapshot-test.pptx").exists()


def test_existing_session_only_never_opens_powerpoint_without_owned_snapshot(tmp_path, monkeypatch):
    renderer.shutdown()
    renderer._failed_until.clear()
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    src = tmp_path / "no-session.pptx"
    src.write_bytes(b"dummy")
    get_app_calls = []

    def forbidden_get_app():
        get_app_calls.append(True)
        raise AssertionError("reuse-only prefetch must not start or attach to PowerPoint")

    monkeypatch.setattr(renderer, "_get_app", forbidden_get_app)

    out = renderer.render_page(
        str(src),
        2,
        cache_key="no-session",
        long_edge=960,
        use_snapshot=True,
        existing_session_only=True,
    )

    assert out is None
    assert get_app_calls == []


def test_active_powerpoint_fails_closed_without_getting_user_app(
    tmp_path,
    monkeypatch,
):
    renderer.shutdown()
    renderer._failed_until.clear()
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    monkeypatch.setattr(renderer, "_powerpoint_active", lambda **_kwargs: True)
    src = tmp_path / "user-session-active.pptx"
    src.write_bytes(b"dummy")
    get_app_calls = []
    def forbidden_get_app():
        get_app_calls.append(True)
        raise AssertionError("preview must not reuse the user's PowerPoint process")

    monkeypatch.setattr(renderer, "_get_app", forbidden_get_app)

    out = renderer._render_page_direct(
        str(src),
        1,
        cache_key="user-session-active",
        long_edge=961,
        hi_priority=True,
        use_snapshot=True,
    )

    assert out is None
    assert get_app_calls == []


def test_powerpoint_session_busy_returns_none_without_poisoning_retry_cache(
    tmp_path,
    monkeypatch,
):
    renderer.shutdown()
    renderer._failed_until.clear()
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    monkeypatch.setattr(renderer, "_powerpoint_active", lambda **_kwargs: False)
    src = tmp_path / "powerpoint-became-busy.pptx"
    src.write_bytes(b"dummy")

    def busy_get_app():
        raise renderer.PowerPointSessionBusy("PowerPoint became active")

    monkeypatch.setattr(renderer, "_get_app", busy_get_app)

    out = renderer._render_page_direct(
        str(src),
        1,
        cache_key="became-busy",
        long_edge=1920,
        hi_priority=True,
        use_snapshot=True,
    )

    assert out is None
    assert renderer._failed_until == {}
    assert not list(tmp_path.glob("became-busy_*.png"))


def test_existing_session_only_reuses_the_owned_snapshot_without_reopening(tmp_path, monkeypatch):
    renderer.shutdown()
    renderer._failed_until.clear()
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    src = tmp_path / "reuse.pptx"
    src.write_bytes(b"dummy")
    app = _FakeApp()
    opens = []
    original_open = app.Presentations.Open

    def recording_open(path, ReadOnly=1, WithWindow=0):
        opens.append(path)
        return original_open(path, ReadOnly=ReadOnly, WithWindow=WithWindow)

    app.Presentations.Open = recording_open
    monkeypatch.setattr(renderer, "_get_app", lambda: app)

    first = renderer.render_page(
        str(src),
        1,
        cache_key="reuse",
        long_edge=960,
        use_snapshot=True,
    )
    monkeypatch.setattr(
        renderer,
        "_get_app",
        lambda: (_ for _ in ()).throw(AssertionError("must reuse existing app")),
    )
    second = renderer.render_page(
        str(src),
        2,
        cache_key="reuse",
        long_edge=960,
        use_snapshot=True,
        existing_session_only=True,
    )

    assert first is not None and second is not None
    assert len(opens) == 1
    renderer.shutdown()
