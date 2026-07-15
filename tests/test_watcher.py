"""全盘监听：覆盖各盘根（recursive 内核级、不预扫）；handler 跳过系统/缓存目录降噪。"""
from __future__ import annotations

import os

from pptx_finder.versioning.watcher import _Handler, default_watch_paths


def test_watch_covers_all_drives():
    paths = [os.path.normcase(p) for p in default_watch_paths()]
    assert paths, "应至少监听一个盘根"
    user_drive = os.path.normcase(os.path.splitdrive(os.path.expanduser("~"))[0] + os.sep)
    assert user_drive in paths  # 用户所在盘被全盘监听（recursive 覆盖其下任何 PPT）


def test_handler_skips_system_and_cache():
    h = _Handler(lambda p: None)
    h._trigger("C:\\Windows\\System32\\x.pptx")
    h._trigger("C:\\Users\\me\\AppData\\Local\\Temp\\y.pptx")
    h._trigger("C:\\proj\\node_modules\\pkg\\z.pptx")
    assert not h._timers, "系统/缓存目录的 .pptx 应被跳过，不起防抖定时器"
    h._trigger("C:\\Users\\me\\Desktop\\方案.pptx")
    assert h._timers, "用户目录的 .pptx 应进入防抖"
    for t in h._timers.values():
        t.cancel()  # 清理，避免定时器残留触发


def test_handler_allows_explicit_root_inside_skipped_tree():
    h = _Handler(
        lambda p: None,
        roots=["C:\\Users\\me\\AppData\\Local\\Temp\\pptdoctor-e2e"],
    )
    h._trigger("C:\\Users\\me\\AppData\\Local\\Temp\\pptdoctor-e2e\\deck.pptx")
    assert h._timers, "显式监听的 AppData 子目录不应被全盘降噪规则误跳过"
    for t in h._timers.values():
        t.cancel()


def test_handler_still_skips_appdata_when_watching_drive_root():
    h = _Handler(lambda p: None, roots=["C:\\"])
    h._trigger("C:\\Users\\me\\AppData\\Local\\Temp\\deck.pptx")
    assert not h._timers, "默认全盘监听仍应跳过 AppData 临时目录降噪"


def test_handler_allows_company_documents_under_appdata_roaming():
    h = _Handler(lambda p: None, roots=["C:\\"])
    h._trigger("C:\\Users\\l00807938\\AppData\\Roaming\\CorpDocs\\deck.pptx")
    try:
        assert h._timers, "AppData\\Roaming 中的正式 PPT 应进入实时索引与版本守护"
    finally:
        for timer in h._timers.values():
            timer.cancel()


def test_handler_never_watches_its_own_data_store(monkeypatch):
    monkeypatch.setattr(
        "pptx_finder.versioning.watcher.data_dir",
        lambda: "C:\\Users\\me\\AppData\\Local\\pptx-finder",
        raising=False,
    )
    h = _Handler(lambda p: None, roots=["C:\\"])
    h._trigger("C:\\Users\\me\\AppData\\Local\\pptx-finder\\objects\\version.pptx")
    assert not h._timers


def test_handler_routes_word_pdf_to_content_index_without_version_snapshot(monkeypatch):
    snapshots = []
    content_changes = []
    monkeypatch.setattr("pptx_finder.versioning.watcher.os.path.exists", lambda _p: True)
    h = _Handler(
        snapshots.append,
        on_content_saved=content_changes.append,
    )

    h._fire("C:\\docs\\report.docx")
    h._fire("C:\\docs\\paper.pdf")
    h._fire("C:\\docs\\deck.pptx")

    assert snapshots == ["C:\\docs\\deck.pptx"]
    assert content_changes == ["C:\\docs\\report.docx", "C:\\docs\\paper.pdf"]


def test_handler_debounces_word_and_pdf_content_changes():
    h = _Handler(lambda _p: None, on_content_saved=lambda _p: None)
    h._trigger("C:\\docs\\report.docx")
    h._trigger("C:\\docs\\paper.pdf")
    assert len(h._timers) == 2
    for timer in h._timers.values():
        timer.cancel()


def test_bulk_save_burst_uses_one_shared_debounce_timer(monkeypatch):
    created = []

    class FakeTimer:
        def __init__(self, delay, callback, args=()):
            self.delay = delay
            self.callback = callback
            self.args = args
            self.daemon = False
            created.append(self)

        def start(self):
            return None

        def cancel(self):
            return None

    monkeypatch.setattr("pptx_finder.versioning.watcher.threading.Timer", FakeTimer)
    h = _Handler(lambda _p: None)

    for index in range(200):
        h._trigger(f"C:\\docs\\deck-{index}.pptx")

    assert len(h._timers) == 200
    assert len(created) == 1, "a burst must not create one OS thread per changed file"
    assert created[0].daemon is True


def test_handler_stop_cancels_all_pending_callbacks():
    h = _Handler(lambda _p: None)
    h._trigger("C:\\docs\\deck.pptx")

    h.stop()

    assert not h._timers


def test_handler_retries_transient_missing_pptx_with_a_hard_limit(monkeypatch):
    """PowerPoint 原子保存的短暂缺口不能直接吞掉，同时重试必须有上限。"""
    callbacks = []
    scheduled = []
    clock = [100.0]

    class FakeTimer:
        def __init__(self, delay, callback, args=()):
            self.delay = delay
            self.callback = callback
            self.args = args
            scheduled.append(self)

        def start(self):
            return None

        def cancel(self):
            return None

    monkeypatch.setattr("pptx_finder.versioning.watcher.threading.Timer", FakeTimer)
    monkeypatch.setattr(
        "pptx_finder.versioning.watcher.time.monotonic",
        lambda: clock[0],
    )
    monkeypatch.setattr("pptx_finder.versioning.watcher.os.path.exists", lambda _p: False)
    h = _Handler(callbacks.append)

    h._fire("C:\\docs\\atomic-save.pptx")
    while scheduled:
        timer = scheduled.pop(0)
        clock[0] += timer.delay
        timer.callback(*timer.args)

    assert callbacks == []
    assert len(h._retry_delays) == 3
    assert not h._timers
