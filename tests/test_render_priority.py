"""预览优先门：缩略图渲染让路给预览（共享 COM 锁下预览不被一屏缩略图饿死）。

用假 COM（每次渲染 sleep）验证：先排一批缩略图占锁，途中来一个预览，
预览应插队、不被排在所有缩略图之后。
"""
from __future__ import annotations

import threading
import time

from pptx_finder import renderer


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
    renderer._state.app = app
    renderer._state.pres = pres
    renderer._state.pres_path = "C:/tmp/rendered.pptx"
    renderer._state.pres_key = "k"

    renderer.shutdown()

    assert closed == [True]
    assert app.quit_calls == 0
    assert getattr(renderer._state, "app", None) is None


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
