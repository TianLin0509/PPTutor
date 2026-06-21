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
