"""UI 集成测试：搜索→结果→选中→预览请求命中页（注入 stub 渲染器，免 COM）。"""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton

import fixtures_gen as fx

from pptx_finder import actions, db, indexer
from pptx_finder.models import FileResult, SearchHit
import pptx_finder.ui.main_window as main_window_mod
from pptx_finder.ui.main_window import MainWindow


def test_stale_search_hit_is_visibly_labeled(qtbot):
    result = FileResult(
        file_id=1,
        path="C:/docs/report.pdf",
        name="report.pdf",
        ext=".pdf",
        mtime=1.0,
        size=10,
        page_count=1,
        status="error",
        score=1.0,
        name_hit=False,
        hits=[SearchHit(1, "上次成功内容")],
    )
    card = main_window_mod.ResultItem(
        result,
        main_window_mod.theme.tok("cloud"),
        "",
    )
    qtbot.addWidget(card)

    badge = card.findChild(QLabel, "staleIndexBadge")
    assert badge is not None
    assert badge.text() == "上次索引"
    assert "最后一次成功解析" in badge.toolTip()


def _index_multi(tmp_path, files: dict[str, list[str]]):
    docs = tmp_path / "d"
    docs.mkdir()
    for fn, bodies in files.items():
        fx.make_pptx(docs / fn, [{"body": b} for b in bodies])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    return conn


class StubRender(QObject):
    """假渲染器：记录请求并立即回 ''（触发 UI 的「无法预览」兜底分支）。"""
    rendered = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[int, str, int]] = []

    def request(self, req_id: int, path: str, page_no: int, cache_key=None):
        self.calls.append((req_id, path, page_no))
        self.rendered.emit(req_id, "")


class ClearableRender(StubRender):
    def __init__(self):
        super().__init__()
        self.clears = 0

    def clear(self):
        self.clears += 1


class PendingRender(QObject):
    rendered = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[int, str, int]] = []

    def request(self, req_id: int, path: str, page_no: int, cache_key=None):
        self.calls.append((req_id, path, page_no))


class StubThumb(QObject):
    thumb_rendered = Signal(str, int, str)

    def __init__(self):
        super().__init__()
        self.requests: list[tuple[str, int]] = []
        self.clears = 0

    def request(self, path: str, page_no: int):
        self.requests.append((path, page_no))

    def clear(self):
        self.clears += 1


class PendingSearchWorker:
    def __init__(self):
        self.requests: list[tuple[int, str, str]] = []
        self.cancels = 0

    def request(self, req_id: int, query: str, mode_key: str, exts=None):
        self.requests.append((req_id, query, mode_key))

    def cancel(self):
        self.cancels += 1

    def stop(self):
        pass

    def wait(self, _ms: int):
        return True


class ObservingSearchWorker(PendingSearchWorker):
    def __init__(self, observer):
        super().__init__()
        self._observer = observer
        self.observed: list[tuple[int, int, int]] = []

    def request(self, req_id: int, query: str, mode_key: str, exts=None):
        self.observed.append(self._observer())
        super().request(req_id, query, mode_key, exts)


def _index(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    fx.make_pptx(docs / "算力方案v2.pptx", [{"body": "第一页封面"}, {"body": "第二页 昇腾 集群部署"}])
    fx.make_pptx(docs / "周报.pptx", [{"body": "本周进展无关词"}])
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    return conn


def test_primary_ui_text_stays_readable_chinese(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    title_buttons = [
        win.findChild(QPushButton, "winMin"),
        win.findChild(QPushButton, "winMax"),
        win.findChild(QPushButton, "winClose"),
    ]

    texts = [
        win.windowTitle(),
        win.search_box.placeholderText(),
        win.search_box.toolTip(),
        win._clear_act.toolTip(),
        *[win.mode.itemText(i) for i in range(win.mode.count())],
        win.facet_btn.text(),
        win.facet_btn.toolTip(),
        win.detail_btn.text(),
        win.detail_btn.toolTip(),
        *[win.sort_combo.itemText(i) for i in range(win.sort_combo.count())],
        win.sort_combo.toolTip(),
        win.theme_btn.toolTip(),
        win.path_label.text(),
        win.copy_path_btn.text(),
        win.image_label.text(),
        win.prev_btn.text(),
        win.next_btn.text(),
        win.goto_btn.text(),
        win.open_btn.text(),
        win.folder_btn.text(),
        win.clip_btn.text(),
        win.clip_btn.toolTip(),
        win.status_label.text(),
        win.hotkey_label.text(),
        *[button.text() for button in title_buttons if button is not None],
        *[button.toolTip() for button in title_buttons if button is not None],
    ]
    joined = "\n".join(texts)
    assert "PPT Doctor" in joined

    assert "PPT 查询助手" in joined
    assert "搜索 PPT 内容" in joined
    assert "只搜短语" in joined
    assert "筛选" in joined
    assert "打开文件" in joined
    assert "复制到剪贴板" in joined
    assert "选中左侧结果" in joined
    assert "切换界面主题" in joined  # 主题按钮 tooltip 固定前缀（与当前持久化主题无关，避免顺序耦合）
    assert "最小化" in joined
    assert "最大化 / 还原" in joined
    assert "关闭" in joined
    assert not any(token in joined for token in ("鏁", "鍚", "绱", "锛", "鈥", "鈫", "馃", "???", "\ufffd"))


def _fake_results(n: int) -> list[FileResult]:
    return [
        FileResult(
            file_id=i,
            path=f"C:/deck-{i}.pptx",
            name=f"deck-{i}.pptx",
            ext=".pptx",
            mtime=1_700_000_000 - i,
            size=1024,
            page_count=1,
            status="ok",
            score=float(n - i),
            name_hit=False,
        )
        for i in range(n)
    ]


class _FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, value=None):
        for callback in list(self.callbacks):
            if value is None:
                callback()
            else:
                callback(value)


def _install_fake_background_task(monkeypatch):
    tasks = []

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = _FakeSignal()
            self.finished = _FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)
    return tasks


def _finish_fake_task(task):
    result = task.fn()
    task.done.emit(result)
    task.finished.emit()
    return result


def test_settings_button_opens_settings_callback(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    calls = []
    win._open_settings_cb = lambda: calls.append("open")

    qtbot.mouseClick(win.settings_btn, Qt.LeftButton)

    assert calls == ["open"]


def test_detail_button_lives_in_preview_actions(qtbot, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    assert win.detail_btn.objectName() == "detailAction"
    assert win.detail_btn.parent().objectName() == "previewHeadBar"
    assert win.detail_btn.isHidden()

    win.search_box.setText("\u6607\u817e")
    win._do_search()
    win.result_list.setCurrentRow(0)

    assert not win.detail_btn.isHidden()
    assert not win.copy_path_btn.isHidden()


def test_double_click_result_opens_hit_page(qtbot, monkeypatch, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    opened = []
    monkeypatch.setattr(win, "_open_at_page_bg", lambda path, page: opened.append((path, page)))

    win.search_box.setText("\u6607\u817e")
    win._do_search()
    item = win.result_list.item(0)
    widget = win.result_list.itemWidget(item)

    widget.activated.emit()

    assert opened == [(win._cur.path, 2)]


def test_search_select_preview(qtbot, tmp_path):
    conn = _index(tmp_path)
    stub = StubRender()
    win = MainWindow(conn=conn, render_worker=stub, do_index=False)
    qtbot.addWidget(win)

    # 内容搜索 → 命中 1 个文件
    win.search_box.setText("昇腾")
    win._do_search()
    assert win.result_list.count() == 1

    # 选中 → 预览请求应指向命中页（第 2 页）
    win.result_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: len(stub.calls) >= 1, timeout=2000)
    assert stub.calls[-1][2] == 2
    # 渲染回空串 → 明确报告 COM 原图失败，不展示替代内容
    assert "暂时无法生成原图预览" in win.image_label.text()


def test_auto_preview_from_search_is_delayed_and_canceled(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_AUTO_PREVIEW_DELAY_MS", 40)
    conn = _index(tmp_path)
    stub = StubRender()
    win = MainWindow(conn=conn, render_worker=stub, do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText("昇腾")
    win._do_search()
    assert stub.calls == []

    win.search_box.setText("不存在的新查询")
    win._do_search()
    qtbot.wait(90)

    assert stub.calls == []


def test_auto_preview_uses_restartable_timer(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_AUTO_PREVIEW_DELAY_MS", 20)
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(win, "_run_auto_preview", lambda token, seq: calls.append((token, seq)))

    win._schedule_auto_preview(1)
    win._schedule_auto_preview(2)
    win._schedule_auto_preview(3)

    qtbot.waitUntil(lambda: bool(calls), timeout=1000)
    qtbot.wait(80)

    assert calls == [(win._auto_preview_token, 3)]


def test_auto_selected_result_clears_stale_preview_while_delayed(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_AUTO_PREVIEW_DELAY_MS", 80)
    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    win.image_label.setText("old-preview")

    win.search_box.setText("昇腾")
    win._do_search()

    assert render.calls == []
    assert win.image_label.text() != "old-preview"


def test_sort_change_auto_preview_is_delayed(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_AUTO_PREVIEW_DELAY_MS", 80)
    conn = _index_multi(
        tmp_path,
        {f"deck-{i}.pptx": ["共同词 算力 集群"] for i in range(3)},
    )
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText("共同词")
    win._do_search()
    render.calls.clear()

    win._on_sort_changed()

    assert render.calls == []
    qtbot.waitUntil(lambda: len(render.calls) == 1, timeout=1000)


def test_startup_render_prewarm_skips_when_user_is_searching(qtbot, tmp_path):
    conn = _index(tmp_path)
    render = PendingRender()
    prewarms: list[str] = []
    render.prewarm = lambda: prewarms.append("prewarm")
    render.stop = lambda: None
    render.wait = lambda _ms: True
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    win._owns_render = True
    win.search_box.setText("算力")
    win._search_pending_req = 123

    win._maybe_prewarm_render()

    assert prewarms == []


def test_startup_render_prewarm_runs_when_idle(qtbot, tmp_path):
    conn = _index(tmp_path)
    render = PendingRender()
    prewarms: list[str] = []
    render.prewarm = lambda: prewarms.append("prewarm")
    render.stop = lambda: None
    render.wait = lambda _ms: True
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    win._owns_render = True

    win._maybe_prewarm_render()

    assert prewarms == ["prewarm"]


def test_preview_request_uses_adaptive_first_paint_resolution(qtbot, tmp_path):
    class AdaptiveRender(QObject):
        rendered = Signal(int, str)

        def __init__(self):
            super().__init__()
            self.calls: list[tuple[int, str, int, int]] = []

        def request(self, req_id: int, path: str, page_no: int, cache_key=None, long_edge=0):
            self.calls.append((req_id, path, page_no, int(long_edge)))

    conn = _index(tmp_path)
    render = AdaptiveRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    win.resize(1180, 760)
    win._cur = _fake_results(1)[0]
    win._view_page = 1

    win._request_preview()

    assert render.calls
    assert MainWindow._PREVIEW_MIN_EDGE <= render.calls[-1][3] <= MainWindow._PREVIEW_MAX_EDGE
    assert render.calls[-1][3] < 2560


@pytest.mark.parametrize(
    ("ext", "kind"),
    [(".docx", "Word"), (".pdf", "PDF")],
)
def test_non_powerpoint_result_never_enters_com_preview_or_goto(qtbot, monkeypatch, tmp_path, ext, kind):
    conn = _index(tmp_path)
    render = StubRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    result = _fake_results(1)[0]
    result.ext = ext
    result.path = f"C:/document{ext}"
    result.name = f"document{ext}"
    win._cur = result
    win._view_page = 1
    win._set_ops_enabled(True)
    opened = []
    monkeypatch.setattr(win, "_open_file_path", opened.append)

    win._request_preview()
    win._act_goto()

    assert render.calls == []
    assert kind in win.image_label.text()
    assert "不支持页图预览" in win.image_label.text()
    assert win.goto_btn.isEnabled() is False
    assert opened == [result.path]


def test_actions_open_at_page_defensively_bypasses_powerpoint_for_docx(monkeypatch, tmp_path):
    path = tmp_path / "report.docx"
    path.write_bytes(b"doc")
    opened = []
    monkeypatch.setattr(actions, "open_file", lambda value: opened.append(value) or True)

    assert actions.open_at_page(str(path), 7) == (True, False)
    assert opened == [str(path)]


def test_low_resolution_disk_cache_is_not_displayed(qtbot, tmp_path, monkeypatch):
    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._recent_load_inflight_token is None, timeout=1000)
    win._cur = _fake_results(1)[0]
    win._view_page = 1
    requested_edges = []

    def no_sharp_cache(path, page, min_long_edge=1, cache_key=None):
        requested_edges.append(min_long_edge)
        return None

    monkeypatch.setattr(main_window_mod.renderer_mod, "find_cached_render", no_sharp_cache)

    win._request_preview()

    assert requested_edges == [win._preview_long_edge()]
    assert requested_edges[0] >= MainWindow._PREVIEW_MIN_EDGE
    assert win._cur_pixmap is None
    assert render.calls
    assert win._spin_timer.isActive()


def test_failed_com_render_clears_any_stale_image(qtbot, tmp_path):
    image = tmp_path / "embedded-cover.png"
    pm = QPixmap(320, 180)
    pm.fill(Qt.green)
    assert pm.save(str(image))

    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    win._req_id = 51

    win._cur_pixmap = QPixmap(str(image))
    win._on_rendered(51, "")

    assert win._cur_pixmap is None
    assert "暂时无法生成原图预览" in win.image_label.text()


def test_preview_does_not_launch_parallel_text_or_shell_renderer(
    qtbot,
    tmp_path,
    monkeypatch,
):
    conn = _index(tmp_path)
    render = PendingRender()  # accepts requests but deliberately never completes one
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._recent_load_inflight_token is None, timeout=1000)
    result = _fake_results(1)[0]
    result.path = str(tmp_path / "blocked-render.pptx")
    win._cur = result
    win._view_page = 1
    monkeypatch.setattr(
        main_window_mod.renderer_mod,
        "find_cached_render",
        lambda *_args, **_kwargs: None,
    )
    win._request_preview()

    assert render.calls
    assert win._cur_pixmap is None
    assert win._spin_timer.isActive()


def test_unavailable_preview_explains_how_original_image_rendering_is_restored(
    qtbot,
    tmp_path,
):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=PendingRender(), do_index=False)
    qtbot.addWidget(win)

    win._show_preview_unavailable()

    message = win.image_label.text()
    assert "暂时无法生成原图预览" in message
    assert "独立预览引擎" not in message
    assert "无需关闭正在编辑的文稿" in message
    assert "请关闭 PowerPoint" not in message


def test_preview_cache_lookup_uses_index_metadata_without_source_stat(
    qtbot, tmp_path, monkeypatch
):
    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._recent_load_inflight_token is None, timeout=1000)
    result = _fake_results(1)[0]
    result.mtime = 1234.5
    result.size = 6789
    win._cur = result
    win._view_page = 1
    seen = []

    def fake_cached(path, page, min_long_edge=1, cache_key=None):
        seen.append((path, page, cache_key))
        return None

    monkeypatch.setattr(main_window_mod.renderer_mod, "find_cached_render", fake_cached)

    win._request_preview()

    assert seen and seen[0][2]


def test_rapid_uncached_page_turn_keeps_latest_com_request(qtbot, tmp_path, monkeypatch):
    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._recent_load_inflight_token is None, timeout=1000)
    result = _fake_results(1)[0]
    result.page_count = 3
    win._cur = result
    monkeypatch.setattr(
        main_window_mod.renderer_mod,
        "find_cached_render",
        lambda path, page, min_long_edge=1, cache_key=None: None,
    )

    win._view_page = 1
    win._request_preview()
    win._view_page = 2
    win._request_preview()

    assert [call[2] for call in render.calls] == [1, 2]
    assert render.calls[-1][2] == 2


def test_sharp_disk_cache_skips_redundant_com_render(qtbot, tmp_path, monkeypatch):
    image = tmp_path / "cached-sharp.png"
    pm = QPixmap(2560, 1440)
    pm.fill(Qt.green)
    assert pm.save(str(image))

    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win._recent_load_inflight_token is None, timeout=1000)
    win._cur = _fake_results(1)[0]
    win._view_page = 1
    monkeypatch.setattr(
        main_window_mod.renderer_mod,
        "find_cached_render",
        lambda path, page, min_long_edge=1, cache_key=None: image,
    )

    win._request_preview()

    assert win._cur_pixmap is not None and not win._cur_pixmap.isNull()
    assert render.calls == []


def test_neighbor_prefetch_is_low_priority_and_limited(qtbot, tmp_path):
    class PrefetchRender(QObject):
        rendered = Signal(int, str)

        def __init__(self):
            super().__init__()
            self.prefetches: list[tuple[str, int, int, int]] = []

        def request(self, req_id: int, path: str, page_no: int, cache_key=None):
            pass

        def prefetch(self, path: str, page_no: int, cache_key=None, long_edge=0, priority=0):
            self.prefetches.append((path, page_no, int(long_edge), int(priority)))

        def stop(self):
            pass

        def wait(self, _ms: int):
            return True

    conn = _index(tmp_path)
    render = PrefetchRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    win._owns_render = True
    win._cur = FileResult(
        file_id=1,
        path="C:/deck.pptx",
        name="deck.pptx",
        ext=".pptx",
        mtime=0,
        size=1,
        page_count=10,
        status="ok",
        score=1,
        name_hit=False,
        hits=[SearchHit(8, "hit"), SearchHit(9, "hit")],
    )
    win._view_page = 3

    win._prefetch_neighbors()

    assert render.prefetches == [
        ("C:/deck.pptx", 4, win._PREFETCH_EDGE, win._PRIORITY_NEIGHBOR_PREFETCH),
        ("C:/deck.pptx", 5, win._PREFETCH_EDGE, win._PRIORITY_NEIGHBOR_PREFETCH),
        ("C:/deck.pptx", 6, win._PREFETCH_EDGE, win._PRIORITY_NEIGHBOR_PREFETCH),
    ]
    assert len(render.prefetches) == win._NEIGHBOR_PREFETCH_MAX == 3
    assert win._PRIORITY_NEIGHBOR_PREFETCH > win._PRIORITY_RIGHT_PREVIEW


def test_completed_right_preview_stays_in_selected_preview_only(qtbot, tmp_path):
    image = tmp_path / "preview.png"
    pm = QPixmap(160, 90)
    pm.fill(Qt.green)
    assert pm.save(str(image), "PNG")

    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    result = _fake_results(1)[0]
    result.hits = [SearchHit(3, "hit")]
    result.page_count = 5
    card = main_window_mod.ResultItem(result, win._tok, "")
    qtbot.addWidget(card)
    win._cur = result
    win._cur_item_widget = card
    win._view_page = 3
    win._req_id = 77

    win._on_rendered(77, str(image))

    assert win._cur_pixmap is not None and not win._cur_pixmap.isNull()
    assert not hasattr(win, "_thumb_cache")
    assert card.findChild(QLabel, "cardThumb") is None


def test_facet_change_auto_preview_is_delayed(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_AUTO_PREVIEW_DELAY_MS", 80)
    conn = _index_multi(
        tmp_path,
        {f"deck-{i}.pptx": ["共同词 算力 集群"] for i in range(3)},
    )
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText("共同词")
    win._do_search()
    qtbot.waitUntil(lambda: len(render.calls) == 1, timeout=1000)
    render.calls.clear()

    win._apply_facet({"page": {"1-10"}})

    assert render.calls == []
    qtbot.waitUntil(lambda: len(render.calls) == 1, timeout=1000)


def test_stale_render_after_empty_search_is_ignored(qtbot, tmp_path):
    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText("昇腾")
    win._do_search()
    qtbot.waitUntil(lambda: len(render.calls) >= 1, timeout=1000)
    old_req = render.calls[-1][0]

    win.search_box.setText("不存在的新查询")
    win._do_search()
    assert win._cur is None

    win.image_label.setText("empty-state")
    render.rendered.emit(old_req, "")

    assert win.image_label.text() == "empty-state"


def test_stale_render_after_empty_facet_is_ignored(qtbot, tmp_path):
    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText("昇腾")
    win._do_search()
    qtbot.waitUntil(lambda: len(render.calls) >= 1, timeout=1000)
    old_req = render.calls[-1][0]

    win.image_label.setText("stale-preview")
    win._apply_facet({"page": {"30+"}})

    assert win._cur is None
    assert win.image_label.text() != "stale-preview"
    empty_text = win.image_label.text()

    render.rendered.emit(old_req, "")

    assert win.image_label.text() == empty_text


def test_render_signal_ignored_after_closing(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=PendingRender(), do_index=False)
    qtbot.addWidget(win)
    win._req_id = 42
    win._closing = True
    win.image_label.setText("closing-preview")

    win._on_rendered(42, "")

    assert win.image_label.text() == "closing-preview"


def test_invalid_preview_image_shows_failure_instead_of_loading(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=PendingRender(), do_index=False)
    qtbot.addWidget(win)
    broken = tmp_path / "broken-preview.png"
    broken.write_text("not a png", encoding="utf-8")
    win._req_id = 7
    win.image_label.setText("正在渲染预览…")
    win._start_spinner()

    win._on_rendered(7, str(broken))

    assert not win._spin_timer.isActive()
    assert "暂时无法生成原图预览" in win.image_label.text()
    assert win._cur_pixmap is None


def test_query_hint_explains_current_search(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText('"昇腾" 集群')
    win._do_search()

    assert not win.query_hint.isHidden()
    assert "精确短语：昇腾" in win.query_hint.text()
    assert "同页包含：集群" in win.query_hint.text()
    assert "多条件为 AND" in win.query_hint.text()


def test_async_pending_keeps_existing_results_visible(qtbot, tmp_path):
    conn = _index(tmp_path)
    stub = StubRender()
    win = MainWindow(conn=conn, render_worker=stub, do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: len(stub.calls) >= 1, timeout=2000)
    old_preview = win.path_label.text()
    old_count = win.result_list.count()

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("不存在的新查询")
    win._do_search()

    assert pending.requests
    assert win.result_list.count() == old_count
    assert win.path_label.text() == old_preview
    assert "搜索中" in win.result_count.text()
    assert not win.empty_hint.isVisible()


def test_async_pending_disables_stale_result_actions(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText("\u6607\u817e")
    win._do_search()
    win.result_list.setCurrentRow(0)
    assert win.open_btn.isEnabled()
    assert win.goto_btn.isEnabled()
    assert win.copy_path_btn.isEnabled()

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("new-query")
    win._do_search()

    assert pending.requests
    assert win.result_list.count() > 0
    assert "\u5f53\u524d\u7ed3\u679c\u6682\u7559" in win.result_count.text()
    assert not win.open_btn.isEnabled()
    assert not win.goto_btn.isEnabled()
    assert not win.folder_btn.isEnabled()
    assert not win.clip_btn.isEnabled()
    assert not win.copy_path_btn.isEnabled()
    opened = []
    monkeypatch.setattr(win, "_open_file_path", lambda path: opened.append(path))
    win._act_open()
    assert opened == []


def test_async_search_failure_reenables_retained_result_actions(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText("\u6607\u817e")
    win._do_search()
    win.result_list.setCurrentRow(0)

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("slow")
    win._do_search()
    req_id, query, _mode = pending.requests[-1]

    win._on_search_done(req_id, query, [], 12.0, "boom")

    assert "\u641c\u7d22\u5931\u8d25" in win.result_count.text()
    assert win.open_btn.isEnabled()
    assert win.goto_btn.isEnabled()
    assert win.folder_btn.isEnabled()
    assert win.clip_btn.isEnabled()


def test_async_search_lock_failure_is_explained_without_technical_jargon(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("AI SP")
    win._do_search()
    req_id, query, _mode = pending.requests[-1]

    win._on_search_done(
        req_id,
        query,
        [],
        401.0,
        "OperationalError: database is locked",
    )

    assert "后台收尾" in win.status_label.text()
    assert "稍后再搜一次" in win.status_label.text()
    assert "database is locked" not in win.status_label.text()


def test_async_pending_blocks_stale_hit_navigation(qtbot, tmp_path):
    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    result = FileResult(
        file_id=1,
        path="C:/multi-hit.pptx",
        name="multi-hit.pptx",
        ext=".pptx",
        mtime=1,
        size=1,
        page_count=5,
        status="ok",
        score=1.0,
        name_hit=False,
        hits=[SearchHit(1, "one"), SearchHit(3, "three")],
    )
    win._showing_recent = False
    win._results_raw = [result]
    win._results = [result]
    win._render_results([result])
    win.result_list.setCurrentRow(0)
    before_calls = len(render.calls)

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("new-query")
    win._do_search()

    assert pending.requests
    assert len(win._thumb_btns) == 2
    assert not win._thumb_btns[1].isEnabled()

    win._goto_hit(1)

    assert win._hit_idx == 0
    assert win._view_page == 1
    assert len(render.calls) == before_calls


def test_async_pending_blocks_stale_wheel_preview_navigation(qtbot, tmp_path):
    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    result = FileResult(
        file_id=1,
        path="C:/wheel-old.pptx",
        name="wheel-old.pptx",
        ext=".pptx",
        mtime=1,
        size=1,
        page_count=5,
        status="ok",
        score=1.0,
        name_hit=False,
    )
    win._showing_recent = False
    win._results_raw = [result]
    win._results = [result]
    win._render_results([result])
    win.result_list.setCurrentRow(0)
    before_calls = len(render.calls)

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("new-query")
    win._do_search()

    win._wheel_page(-120)

    assert win._view_page == 1
    assert len(render.calls) == before_calls


def test_async_pending_blocks_stale_preview_zoom(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    pm = QPixmap(32, 24)
    pm.fill(QColor(10, 20, 30))
    win._cur_pixmap = pm
    win._zoom = 1.0

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("new-query")
    win._do_search()

    win._zoom_by(1.15)
    assert win._zoom == 1.0

    win._toggle_zoom()

    assert pending.requests
    assert win._zoom == 1.0


def test_async_pending_freezes_result_selection_navigation(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    results = _fake_results(2)
    win._showing_recent = False
    win._results_raw = results
    win._results = results
    win._render_results(results)
    win.result_list.setCurrentRow(0)
    first_path = win._cur.path

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("new-query")
    win._do_search()

    assert pending.requests
    assert not win.result_list.isEnabled()

    qtbot.keyClick(win.search_box, Qt.Key_Down)

    assert win.result_list.currentRow() == 0
    assert win._cur.path == first_path


def test_async_pending_blocks_sort_from_reenabling_stale_results(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    results = [
        FileResult(file_id=1, path="C:/b.pptx", name="b.pptx", ext=".pptx",
                   mtime=1, size=1, page_count=1, status="ok", score=2.0, name_hit=False),
        FileResult(file_id=2, path="C:/a.pptx", name="a.pptx", ext=".pptx",
                   mtime=2, size=1, page_count=1, status="ok", score=1.0, name_hit=False),
    ]
    win._showing_recent = False
    win._results_raw = results
    win._results = results
    win._render_results(results)
    assert [r.name for r in win._results] == ["b.pptx", "a.pptx"]

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("new-query")
    win._do_search()

    win.sort_combo.setCurrentText("\u6587\u4ef6\u540d")

    assert not win.result_list.isEnabled()
    assert [r.name for r in win._results] == ["b.pptx", "a.pptx"]


def test_async_pending_blocks_facet_from_replacing_stale_results(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    results = _fake_results(2)
    win._showing_recent = False
    win._results_raw = results
    win._results = results
    win._render_results(results)

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("new-query")
    win._do_search()

    win._apply_facet({"page": {"30+"}})

    assert not win.result_list.isEnabled()
    assert win.result_list.count() == 2
    assert "\u641c\u7d22\u4e2d" in win.result_count.text()
    assert win.empty_hint.isHidden()


def test_async_pending_slow_hint_keeps_old_results_actionable(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_SEARCH_SLOW_HINT_MS", 20)
    conn = _index(tmp_path)
    stub = StubRender()
    win = MainWindow(conn=conn, render_worker=stub, do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText("昇腾")
    win._do_search()
    old_count = win.result_list.count()

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("很慢的新查询")
    win._do_search()

    qtbot.waitUntil(lambda: "仍在进行" in win.result_count.text(), timeout=1000)

    assert win.result_list.count() == old_count
    assert "当前结果暂留" in win.result_count.text()
    assert "可继续输入" in win.result_count.text()


def test_async_pending_slow_hint_without_old_results(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_SEARCH_SLOW_HINT_MS", 20)
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.result_list.clear()
    win._results = []
    win._results_raw = []
    win.search_box.setText("首次慢查询")
    win._do_search()

    qtbot.waitUntil(lambda: "仍在进行" in win.result_count.text(), timeout=1000)

    assert win.result_list.count() == 0
    assert "可继续输入" in win.result_count.text()


def test_search_slow_hint_uses_restartable_timer(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_SEARCH_SLOW_HINT_MS", 20)
    conn = _index(tmp_path)
    pending = PendingSearchWorker()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._search_worker = pending
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(win, "_show_search_slow_hint", lambda req_id, query: calls.append((req_id, query)))

    for query in ("first", "second", "third"):
        win.search_box.setText(query)
        win._do_search()

    qtbot.waitUntil(lambda: bool(calls), timeout=1000)
    qtbot.wait(80)

    assert calls == [(win._search_seq, "third")]


def test_scope_suggestion_runs_single_search_request(qtbot, tmp_path):
    conn = _index(tmp_path)
    pending = PendingSearchWorker()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.mode.blockSignals(True)
    win.mode.setCurrentIndex(2)
    win.mode.blockSignals(False)
    win._search_worker = pending
    win.search_box.setText("missing")
    win._debounce.stop()

    win._apply_suggestion("allmode")

    assert win.mode.currentIndex() == 0
    assert len(pending.requests) == 1
    assert pending.requests[0][1] == "missing"
    assert pending.requests[0][2] == "all"


def test_async_search_success_refreshes_status_after_pending(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    pending = PendingSearchWorker()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._search_worker = pending
    refreshes = []

    def fake_refresh_status(summary=None):
        refreshes.append(summary)
        win.status_label.setText("ready")

    monkeypatch.setattr(win, "_refresh_status", fake_refresh_status)

    win.search_box.setText("slow")
    win._do_search()
    req_id, query, _mode = pending.requests[-1]

    win._on_search_done(req_id, query, _fake_results(1), 12.0, None)

    assert refreshes == [None]
    assert win.status_label.text() == "ready"


def test_live_refresh_waits_for_pending_search(qtbot, tmp_path):
    conn = _index(tmp_path)
    pending = PendingSearchWorker()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._search_worker = pending

    win.search_box.setText("昇腾")
    win._do_search()
    first_req = pending.requests[-1][0]

    win._do_live_refresh()

    assert len(pending.requests) == 1
    assert win._live_refresh_after_search is True

    # Production uses a multi-second quiet period.  Shorten only this test's
    # timer while preserving the wait-until-the-current-search-finishes rule.
    win._live_refresh.setInterval(20)
    win._on_search_done(first_req, "昇腾", [], 1.0, None)

    qtbot.waitUntil(lambda: len(pending.requests) == 2, timeout=1000)
    assert pending.requests[-1][1] == "昇腾"


def test_live_index_status_refresh_is_debounced(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_LIVE_STATUS_REFRESH_MS", 20, raising=False)
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    calls = []

    monkeypatch.setattr(win, "_refresh_status", lambda summary=None: calls.append(summary))

    win._after_live_index()
    win._after_live_index()
    win._after_live_index()

    assert calls == []
    qtbot.waitUntil(lambda: len(calls) == 1, timeout=1000)


def test_refresh_status_reads_stats_in_background(qtbot, monkeypatch, tmp_path):
    tasks = _install_fake_background_task(monkeypatch)
    calls = []

    def fake_stats(_conn, **_kwargs):
        calls.append("stats")
        return {"file_count": 7, "page_count": 11}

    monkeypatch.setattr(main_window_mod.db, "stats", fake_stats)
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    tasks.clear()
    calls.clear()

    win._refresh_status({"indexed": 2, "deleted": 1})

    assert calls == []
    assert tasks and tasks[-1].label == "index-status-refresh"

    _finish_fake_task(tasks[-1])

    assert calls == ["stats"]
    assert "索引就绪：7 个文件 · 11 页" in win.status_label.text()
    # 设计 F：就绪态显示类型分布（PPT…），不再显示「更新 N，移除 M」黑话
    assert "PPT" in win.status_label.text()
    assert "更新" not in win.status_label.text()


def test_refresh_status_reuses_inflight_background_task(qtbot, monkeypatch, tmp_path):
    tasks = _install_fake_background_task(monkeypatch)
    calls = []

    def fake_stats(_conn, **_kwargs):
        calls.append("stats")
        return {"file_count": 5, "page_count": 8}

    monkeypatch.setattr(main_window_mod.db, "stats", fake_stats)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    for task in list(tasks):
        _finish_fake_task(task)
    tasks.clear()
    calls.clear()

    win._refresh_status()
    first_task = tasks[-1]
    win._refresh_status()
    win._refresh_status()

    status_tasks = [task for task in tasks if task.label == "index-status-refresh"]
    assert status_tasks == [first_task]
    assert calls == []

    _finish_fake_task(first_task)

    assert calls == ["stats"]
    assert "索引就绪：5 个文件 · 8 页" in win.status_label.text()


def test_stale_status_refresh_does_not_override_search_pending(qtbot, monkeypatch, tmp_path):
    tasks = _install_fake_background_task(monkeypatch)
    monkeypatch.setattr(
        main_window_mod.db,
        "stats",
        lambda _conn, **_kwargs: {"file_count": 2, "page_count": 3},
    )
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    for task in list(tasks):
        _finish_fake_task(task)
    tasks.clear()

    win._refresh_status()
    status_task = tasks[-1]
    win._show_search_pending("昇腾")

    _finish_fake_task(status_task)

    assert "正在搜索" in win.status_label.text()


def test_index_done_status_refresh_does_not_override_search_pending(qtbot, monkeypatch, tmp_path):
    tasks = _install_fake_background_task(monkeypatch)
    monkeypatch.setattr(
        main_window_mod.db,
        "stats",
        lambda _conn, **_kwargs: {"file_count": 7, "page_count": 11},
    )
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    for task in list(tasks):
        _finish_fake_task(task)
    tasks.clear()

    win.search_box.setText("昇腾")
    win._show_search_pending("昇腾")
    win._on_index_done({"indexed": 1, "deleted": 0})
    status_task = next(task for task in reversed(tasks) if task.label == "index-status-refresh")
    _finish_fake_task(status_task)

    assert "正在搜索" in win.status_label.text()


def test_index_progress_does_not_override_search_pending(qtbot, monkeypatch, tmp_path):
    tasks = _install_fake_background_task(monkeypatch)
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    for task in list(tasks):
        _finish_fake_task(task)

    win.search_box.setText("昇腾")
    win._show_search_pending("昇腾")
    win._on_index_progress(3, 10, "C:/docs/deck.pptx")

    assert "正在搜索" in win.status_label.text()
    assert win.index_bar.value() == 3


def test_deferred_live_refresh_does_not_search_after_closing(qtbot, tmp_path):
    conn = _index(tmp_path)
    pending = PendingSearchWorker()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._search_worker = pending
    win.search_box.setText("昇腾")
    win._live_refresh_after_search = True

    win._maybe_run_deferred_live_refresh("昇腾")
    win._closing = True
    qtbot.wait(40)

    assert pending.requests == []


def test_deferred_live_refresh_waits_for_idle_before_rerun(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._live_refresh_after_search = True

    win._maybe_run_deferred_live_refresh("昇腾")

    assert win._live_refresh.isActive()
    assert win._live_refresh.interval() >= 1000
    assert win._live_refresh_after_search is False


def test_new_search_clears_preview_queue_without_touching_legacy_thumb_worker(qtbot, tmp_path):
    conn = _index(tmp_path)
    render = ClearableRender()
    thumb = StubThumb()
    win = MainWindow(conn=conn, render_worker=render, thumb_worker=thumb, do_index=False)
    qtbot.addWidget(win)
    pending = ObservingSearchWorker(lambda: (render.clears, thumb.clears, win._req_id))
    win._search_worker = pending
    render.clears = 0
    thumb.clears = 0
    old_req_id = win._req_id
    old_render_gen = win._render_gen

    old_block = win.search_box.blockSignals(True)
    win.search_box.setText("new query")
    win.search_box.blockSignals(old_block)
    win._do_search()

    assert pending.requests == [(win._search_seq, "new query", "all")]
    assert pending.observed == [(1, 0, old_req_id + 1)]
    assert render.clears == 1
    assert thumb.clears == 0
    assert win._render_gen == old_render_gen + 1


def test_old_preview_result_is_ignored_after_new_search(qtbot, tmp_path):
    conn = _index(tmp_path)
    render = ClearableRender()
    thumb = StubThumb()
    win = MainWindow(conn=conn, render_worker=render, thumb_worker=thumb, do_index=False)
    qtbot.addWidget(win)
    win._search_worker = PendingSearchWorker()
    old_req_id = win._req_id

    old_block = win.search_box.blockSignals(True)
    win.search_box.setText("new query")
    win.search_box.blockSignals(old_block)
    win._do_search()
    render.rendered.emit(old_req_id, "C:/stale-preview.png")

    assert win._cur_pixmap is None


def test_large_result_rendering_batches_first_frame_without_thumbnails(qtbot, tmp_path):
    conn = _index(tmp_path)
    thumb = StubThumb()
    win = MainWindow(conn=conn, render_worker=StubRender(), thumb_worker=thumb, do_index=False)
    qtbot.addWidget(win)

    win._showing_recent = False
    initial_thumb_requests = len(thumb.requests)
    win._render_results(_fake_results(80))

    assert win._RENDER_FIRST <= 16
    assert win._RENDER_CHUNK <= 16
    assert win.result_list.count() == win._RENDER_FIRST + 1
    assert len(thumb.requests) == initial_thumb_requests

    # The list remains virtualized until the user asks for/scrolls to more.
    qtbot.wait(250)
    assert win.result_list.count() == win._RENDER_FIRST + 1
    while win._render_plan_pos < len(win._render_plan):
        win._load_more_results()
    assert win.result_list.count() == 80
    assert len(thumb.requests) == initial_thumb_requests


def test_result_cards_do_not_have_thumbnail_scheduler_or_cache(qtbot, tmp_path):
    conn = _index(tmp_path)
    thumb = StubThumb()
    win = MainWindow(conn=conn, render_worker=StubRender(), thumb_worker=thumb, do_index=False)
    qtbot.addWidget(win)

    win._render_results(_fake_results(24))

    assert thumb.requests == []
    assert not hasattr(win, "_request_visible_thumbs")
    assert not hasattr(win, "_thumb_cache")
    for row in range(win._RENDER_FIRST):
        card = win.result_list.itemWidget(win.result_list.item(row))
        assert card.findChild(QLabel, "cardThumb") is None


def test_large_index_async_search_streams_without_ui_slow_gap(qtbot, monkeypatch, tmp_path):
    from pptx_finder.config import db_path as cfg_db_path

    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    docs = tmp_path / "large-docs"
    docs.mkdir()
    for i in range(48):
        fx.make_pptx(
            docs / f"deck-{i:02d}.pptx",
            [{"body": f"算力 集群 大样本 异步搜索 {i}"}],
        )
    conn = db.connect(cfg_db_path())
    try:
        db.init_db(conn)
        indexer.update_index(conn, [str(docs)], workers=1)
    finally:
        conn.close()

    thumb = StubThumb()
    win = MainWindow(conn=None, render_worker=StubRender(), thumb_worker=thumb, do_index=True)
    qtbot.addWidget(win)
    try:
        initial_thumb_requests = len(thumb.requests)
        win.search_box.setText("算力 集群")
        win._do_search()

        qtbot.waitUntil(lambda: len(win._results_raw) == 48, timeout=5000)
        qtbot.waitUntil(
            lambda: win.result_list.count() == win._RENDER_FIRST + 1,
            timeout=5000,
        )

        lines = "\n".join(win.diagnostic_lines())
        assert "ui_loop:" in lines
        assert "slow_gaps=0" in lines
        assert len(thumb.requests) == initial_thumb_requests
        assert win._search_worker is not None
        assert win._search_worker.diagnostics()["total"] >= 1
    finally:
        win.close()


def test_stream_rendering_uses_positive_yield_between_batches(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    scheduled: list[int] = []

    def fake_single_shot(delay_ms: int, _callback):
        scheduled.append(delay_ms)

    monkeypatch.setattr(main_window_mod.QTimer, "singleShot", fake_single_shot)

    win._stream_plan_rest([("i", i, r) for i, r in enumerate(_fake_results(20))], 0, "", win._render_gen)

    assert scheduled
    assert scheduled[0] >= 1


def test_ui_loop_diagnostics_records_max_gap(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win._ui_loop_last_tick = 1000.0
    win._record_ui_loop_tick(now=1000.0 + (win._UI_LOOP_INTERVAL_MS / 1000.0) + 0.9)

    lines = "\n".join(win.diagnostic_lines())
    assert "ui_loop:" in lines
    assert "max_gap=900 ms" in lines
    assert "slow_gaps=1" in lines


def test_diagnostics_include_render_but_not_legacy_thumb_worker_lines(qtbot, tmp_path):
    conn = _index(tmp_path)

    class RenderWithDiagnostics(StubRender):
        def diagnostic_lines(self):
            return ["render_worker: preview_pending=False", "render_worker_stats: preview=1/1"]

    class ThumbWithDiagnostics(StubThumb):
        def diagnostic_lines(self):
            return ["thumb_worker: queued=0 active=0", "thumb_worker_stats: completed=1/1"]

    win = MainWindow(
        conn=conn,
        render_worker=RenderWithDiagnostics(),
        thumb_worker=ThumbWithDiagnostics(),
        do_index=False,
    )
    qtbot.addWidget(win)

    lines = "\n".join(win.diagnostic_lines())

    assert "render_worker: preview_pending=False" in lines
    assert "thumb_worker: queued=0 active=0" not in lines


def test_ui_loop_diagnostics_flags_noticeable_mid_sized_gap(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win._ui_loop_last_tick = 2000.0
    win._record_ui_loop_tick(now=2000.0 + (win._UI_LOOP_INTERVAL_MS / 1000.0) + 0.3)

    lines = "\n".join(win.diagnostic_lines())
    assert "max_gap=300 ms" in lines
    assert "slow_gaps=1" in lines
    assert "threshold=250 ms" in lines


def test_diagnostics_include_active_index_progress(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    class RunningIndexer:
        def isRunning(self):
            return True

        def stop(self):
            pass

        def wait(self, _ms):
            return True

    win._indexer = RunningIndexer()
    win._on_index_progress(3, 10, r"C:\docs\deck.pptx")

    lines = "\n".join(win.diagnostic_lines())

    assert "index_active:" in lines
    assert "done=3 total=10" in lines
    assert "deck.pptx" in lines


def test_heavy_background_operations_are_gated(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    calls: list[str] = []

    def slow():
        calls.append("first")
        time.sleep(0.2)
        return True

    win._run_bg(slow, label="open")
    qtbot.waitUntil(lambda: win._active_heavy_op == "open", timeout=1000)
    win._run_bg(lambda: calls.append("second"), label="restore")

    assert "second" not in calls
    qtbot.waitUntil(lambda: win._active_heavy_op is None, timeout=3000)


def test_heavy_operation_keeps_action_buttons_disabled_after_selection_change(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    def slow():
        time.sleep(0.2)
        return True

    win._render_results(_fake_results(2))
    win.result_list.setCurrentRow(0)
    win._run_bg(slow, label="open")
    qtbot.waitUntil(lambda: win._active_heavy_op == "open", timeout=1000)
    assert not win.open_btn.isEnabled()

    win.result_list.setCurrentRow(1)

    assert not win.open_btn.isEnabled()
    assert not win.goto_btn.isEnabled()
    assert not win.folder_btn.isEnabled()
    assert not win.clip_btn.isEnabled()
    qtbot.waitUntil(lambda: win._active_heavy_op is None, timeout=3000)


def test_active_file_operation_blocks_preview_navigation_and_zoom(qtbot, tmp_path):
    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    result = FileResult(
        file_id=1,
        path="C:/busy-preview.pptx",
        name="busy-preview.pptx",
        ext=".pptx",
        mtime=1,
        size=1,
        page_count=5,
        status="ok",
        score=1.0,
        name_hit=False,
        hits=[SearchHit(1, "one"), SearchHit(3, "three")],
    )
    win._showing_recent = False
    win._results_raw = [result]
    win._results = [result]
    win._render_results([result])
    win.result_list.setCurrentRow(0)
    pm = QPixmap(32, 24)
    pm.fill(QColor(10, 20, 30))
    win._cur_pixmap = pm
    win._zoom = 1.0
    before_calls = len(render.calls)

    win._active_heavy_op = "open"
    win._set_ops_enabled(False)
    win._goto_hit(1)
    win._step_hit(1)
    win._wheel_page(-120)
    win._act_goto_page(4)
    win._zoom_by(1.15)
    win._toggle_zoom()

    assert win._hit_idx == 0
    assert win._view_page == 1
    assert win._zoom == 1.0
    assert len(render.calls) == before_calls


def test_active_file_operation_defers_selection_preview_until_finished(qtbot, tmp_path):
    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    results = _fake_results(2)
    for r in results:
        r.page_count = 4
    win._showing_recent = False
    win._results_raw = results
    win._results = results
    win._render_results(results)
    win.result_list.setCurrentRow(0)

    def slow():
        time.sleep(0.2)
        return True

    win._run_bg(slow, label="open")
    qtbot.waitUntil(lambda: win._active_heavy_op == "open", timeout=1000)
    active_calls = len(render.calls)

    win.result_list.setCurrentRow(1)

    assert win._cur.path == results[1].path
    assert len(render.calls) == active_calls

    qtbot.waitUntil(lambda: win._active_heavy_op is None, timeout=3000)
    qtbot.waitUntil(lambda: len(render.calls) > active_calls, timeout=1000)
    assert render.calls[-1][1] == results[1].path


def test_deferred_busy_preview_flushes_after_retained_search_failure(qtbot, tmp_path):
    conn = _index(tmp_path)
    render = PendingRender()
    win = MainWindow(conn=conn, render_worker=render, do_index=False)
    qtbot.addWidget(win)
    results = _fake_results(2)
    for r in results:
        r.page_count = 4
    win._showing_recent = False
    win._results_raw = results
    win._results = results
    win._render_results(results)
    win.result_list.setCurrentRow(0)

    win._active_heavy_op = "open"
    active_calls = len(render.calls)
    win.result_list.setCurrentRow(1)
    assert win._preview_deferred_due_to_busy
    assert len(render.calls) == active_calls

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("slow")
    win._do_search()
    req_id, query, _mode = pending.requests[-1]

    win._active_heavy_op = None
    win._flush_deferred_preview_if_idle()
    assert len(render.calls) == active_calls

    win._on_search_done(req_id, query, [], 12.0, "boom")

    assert not win._preview_deferred_due_to_busy
    assert len(render.calls) == active_calls + 1
    assert render.calls[-1][1] == results[1].path


def test_result_actions_recheck_file_operation_when_triggered(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._showing_recent = False
    win._results_raw = _fake_results(1)
    win._results = list(win._results_raw)
    win._render_results(win._results)
    win.result_list.setCurrentRow(0)
    win.search_box.setText("busy query")
    calls: list[tuple] = []

    monkeypatch.setattr(win, "_open_file_path", lambda path: calls.append(("open", path)))
    monkeypatch.setattr(win, "_open_folder_path", lambda path: calls.append(("folder", path)))
    monkeypatch.setattr(win, "_open_at_page_bg", lambda path, page: calls.append(("goto", path, page)))
    monkeypatch.setattr(win, "_copy_text_with_toast", lambda text, msg: calls.append(("copy_path", text, msg)))
    monkeypatch.setattr(win, "_check_clipboard_file_exists_bg", lambda path, token: calls.append(("exists", path, token)))
    monkeypatch.setattr(win, "_set_file_clipboard", lambda path: calls.append(("set_clip", path)))
    monkeypatch.setattr(win, "_confirm_file_clipboard", lambda path, token, remaining: calls.append(("confirm", path, token, remaining)))
    monkeypatch.setattr(main_window_mod.history, "add_history", lambda query: calls.append(("history", query)))
    monkeypatch.setattr(win, "_refresh_history_model", lambda: calls.append(("refresh_history",)))

    win._active_heavy_op = "open"
    win._act_open()
    win._act_folder()
    win._act_goto()
    win._act_copy_path()
    win._act_copy_clipboard()

    assert calls == []
    assert win._clipboard_copy_token == 0
    assert "已有文件操作正在进行" in win._toast_label.text()


def test_shutdown_uses_short_wait_for_light_background_tasks(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    waits: list[tuple[str, int]] = []

    class FakeTask:
        def __init__(self, label: str):
            self._label = label

        def wait(self, timeout_ms: int):
            waits.append((self._label, timeout_ms))
            return True

    win._bg_tasks = [
        FakeTask("recent-files-load"),
        FakeTask("index-status-refresh"),
        FakeTask("open"),
        FakeTask("restore"),
    ]
    win._owns_thumb = False
    win._owns_render = False
    win._search_worker = None
    win._live = None
    win._indexer = None

    win._shutdown()

    assert ("open", 3000) in waits
    assert ("restore", 3000) in waits
    assert ("recent-files-load", 3000) not in waits
    assert ("index-status-refresh", 3000) not in waits
    assert dict(waits)["recent-files-load"] <= 500
    assert dict(waits)["index-status-refresh"] <= 500


def test_shutdown_also_stops_optional_feature_runtime(qtbot, tmp_path):
    """Every real exit path, including updater-driven exit, must stop watchers."""
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    calls: list[str] = []

    class FakeFeatureRuntime:
        def stop(self):
            calls.append("stop")

    win._feature_runtime = FakeFeatureRuntime()
    win._bg_tasks = []
    win._owns_thumb = False
    win._owns_render = False
    win._search_worker = None
    win._live = None
    win._indexer = None

    win._shutdown()

    assert calls == ["stop"]


def test_shutdown_is_idempotent_when_quit_and_close_paths_overlap(qtbot, tmp_path):
    """Qt close-after-quit must not repeat multi-second worker teardown."""
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    calls = []

    class FakeFeatureRuntime:
        def stop(self):
            calls.append("feature.stop")

    class FakeWorker:
        def stop(self):
            calls.append("worker.stop")

        def wait(self, _timeout_ms):
            calls.append("worker.wait")
            return True

    win._feature_runtime = FakeFeatureRuntime()
    win._search_worker = FakeWorker()
    win._bg_tasks = []
    win._owns_render = False
    win._live = None
    win._indexer = None

    win._shutdown()
    win._shutdown()

    assert calls == ["feature.stop", "worker.stop", "worker.wait"]


def test_window_resize_coalesces_expensive_preview_scaling(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    win._cur_pixmap = QPixmap(1280, 720)
    calls = []
    win._update_pixmap = lambda: calls.append("scale")

    for width in range(1180, 1300, 10):
        win.resize(width, 760)

    assert calls == []
    qtbot.waitUntil(lambda: calls == ["scale"], timeout=500)


def test_completed_scan_discloses_unreadable_folders(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win._apply_status_stats(
        {"unreadable_dirs": 3},
        {"file_count": 2, "page_count": 4, "type_counts": {".pptx": (2, 2)}},
    )

    assert "3 个文件夹无权限" in win.status_label.text()


def test_shutdown_treats_version_file_ops_as_heavy_background_tasks(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    class FakeTask:
        def __init__(self, label: str):
            self._label = label

    for label in (
        "version-restore", "version-export", "version-recover",
        "ppt-slim-analyze", "ppt-slim-create",
    ):
        assert win._bg_task_shutdown_wait_ms(FakeTask(label)) == win._BG_HEAVY_SHUTDOWN_WAIT_MS


def test_shutdown_caps_total_wait_for_many_light_background_tasks(qtbot, tmp_path, monkeypatch):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    waits: list[tuple[str, int]] = []
    clock = [100.0]

    monkeypatch.setattr(main_window_mod.time, "monotonic", lambda: clock[0])

    class FakeTask:
        def __init__(self, label: str):
            self._label = label

        def wait(self, timeout_ms: int):
            waits.append((self._label, timeout_ms))
            clock[0] += max(0, timeout_ms) / 1000.0
            return False

    win._bg_tasks = [FakeTask(f"light-{i}") for i in range(6)]
    win._owns_thumb = False
    win._owns_render = False
    win._search_worker = None
    win._live = None
    win._indexer = None

    win._shutdown()

    assert sum(timeout for _label, timeout in waits) <= win._BG_LIGHT_SHUTDOWN_TOTAL_WAIT_MS
    assert [timeout for _label, timeout in waits][-2:] == [0, 0]


def test_shutdown_uses_short_wait_for_search_worker(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    calls: list[tuple[str, int | None]] = []

    class FakeSearchWorker:
        def stop(self):
            calls.append(("stop", None))

        def wait(self, timeout_ms: int):
            calls.append(("wait", timeout_ms))
            return False

    win._search_worker = FakeSearchWorker()
    win._bg_tasks = []
    win._owns_thumb = False
    win._owns_render = False
    win._live = None
    win._indexer = None

    win._shutdown()

    assert ("stop", None) in calls
    waits = [value for kind, value in calls if kind == "wait"]
    assert waits and waits[-1] <= 500


def test_shutdown_stops_only_selected_preview_render_worker(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    events: list[str] = []

    class FakeRender:
        def stop(self):
            events.append("render.stop")

        def wait(self, _timeout_ms: int):
            events.append("render.wait")
            return True

    win._render = FakeRender()
    win._owns_render = True
    win._search_worker = None
    win._bg_tasks = []
    win._live = None
    win._indexer = None

    win._shutdown()

    assert events == ["render.stop", "render.wait"]


def test_shutdown_never_force_terminates_a_busy_render_thread(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    events = []

    class BusyRender:
        def stop(self):
            events.append("stop")

        def abort_inflight(self):
            events.append("abort")
            return True

        def wait(self, _timeout_ms):
            events.append("wait")
            return len([x for x in events if x == "wait"]) > 1

        def terminate(self):
            events.append("terminate")

    win._render = BusyRender()
    win._owns_render = True
    win._search_worker = None
    win._bg_tasks = []
    win._live = None
    win._indexer = None

    win._shutdown()

    assert "abort" in events
    assert "terminate" not in events


def test_filename_mode_and_multi_term(qtbot, tmp_path):
    conn = _index(tmp_path)
    stub = StubRender()
    win = MainWindow(conn=conn, render_worker=stub, do_index=False)
    qtbot.addWidget(win)

    # 文件名命中
    win.search_box.setText("算力方案")
    win._do_search()
    assert win.result_list.count() == 1
    assert win._results[0].name_hit is True

    # 多词 AND：两个词需同页
    win.search_box.setText("昇腾 集群")
    win._do_search()
    assert win.result_list.count() == 1

    win.search_box.setText("昇腾 不存在的词xyz")
    win._do_search()
    assert win.result_list.count() == 0


def test_empty_shows_recent(qtbot, tmp_path):
    """清空搜索 → 展示最近修改的文件（默认视图），不再是空白。"""
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    assert win.result_list.count() == 1
    win.search_box.setText("")
    win._do_search()
    qtbot.waitUntil(lambda: len(win._results) == 2, timeout=2000)
    assert len(win._results) == 2                # 索引 2 个文件（result_list 含时间分组头）
    assert win._showing_recent is True


def test_empty_search_cancels_async_worker(qtbot, tmp_path):
    conn = _index(tmp_path)
    search_worker = PendingSearchWorker()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._search_worker = search_worker

    win.search_box.setText("slow")
    win._do_search()
    assert search_worker.requests

    win.search_box.setText("")
    win._do_search()

    assert search_worker.cancels == 1


def test_clearing_pending_search_drops_stale_current_before_recent_load(
    qtbot, monkeypatch, tmp_path
):
    conn = _index(tmp_path)
    tasks = _install_fake_background_task(monkeypatch)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    win.search_box.setText("昇腾")
    win._do_search()
    assert win._cur is not None

    search_worker = PendingSearchWorker()
    win._search_worker = search_worker
    win.search_box.setText("new-query")
    win._do_search()
    assert search_worker.requests
    assert win._cur is not None

    opened = []
    monkeypatch.setattr(win, "_open_at_page_bg", lambda path, page: opened.append((path, page)))
    win.search_box.setText("")
    win._do_search()

    assert search_worker.cancels == 1
    assert win._recent_load_inflight_token is not None
    assert tasks[-1].label == "recent-files-load"
    assert win._cur is None
    qtbot.keyClick(win.search_box, Qt.Key_Return)
    assert opened == []


def test_clear_action_cancels_pending_search_immediately(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    search_worker = PendingSearchWorker()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._search_worker = search_worker
    recent_calls = []
    monkeypatch.setattr(win, "_show_recent", lambda *args, **kwargs: recent_calls.append((args, kwargs)))

    win.search_box.setText("slow")
    win._do_search()
    assert search_worker.requests

    win._clear_act.trigger()

    assert win.search_box.text() == ""
    assert search_worker.cancels == 1
    assert recent_calls == [((), {})]
    assert not win._debounce.isActive()


def test_escape_cancels_pending_search_immediately(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    search_worker = PendingSearchWorker()
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._search_worker = search_worker
    recent_calls = []
    monkeypatch.setattr(win, "_show_recent", lambda *args, **kwargs: recent_calls.append((args, kwargs)))

    win.search_box.setText("slow")
    win._do_search()
    assert search_worker.requests

    qtbot.keyClick(win.search_box, Qt.Key_Escape)

    assert win.search_box.text() == ""
    assert search_worker.cancels == 1
    assert recent_calls == [((), {})]
    assert not win._debounce.isActive()


def test_instant_search_debounce(qtbot, tmp_path):
    """输入触发防抖后自动搜索（不手动调 _do_search）。"""
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")  # 仅 setText，靠 textChanged→防抖→自动搜
    qtbot.wait(420)
    assert win.result_list.count() == 1


def test_manual_search_cancels_pending_debounce(qtbot, tmp_path):
    conn = _index(tmp_path)
    thumb = StubThumb()
    win = MainWindow(conn=conn, render_worker=StubRender(), thumb_worker=thumb, do_index=False)
    qtbot.addWidget(win)
    win._debounce.setInterval(20)

    win.search_box.setText("昇腾")
    win._do_search()
    manual_seq = win._search_seq
    thumb_requests = len(thumb.requests)

    qtbot.wait(80)

    assert win._search_seq == manual_seq
    assert len(thumb.requests) == thumb_requests


def test_keyboard_nav(qtbot, tmp_path):
    """search_box 上按 ↑↓ 移动结果选中。"""
    conn = _index_multi(tmp_path, {f"f{i}.pptx": [f"共同词 算力 集群 唯一{i}"] for i in range(3)})
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("算力 集群")
    win._do_search()
    assert win.result_list.count() == 3
    win.result_list.setCurrentRow(0)
    qtbot.keyClick(win.search_box, Qt.Key_Down)
    assert win.result_list.currentRow() == 1
    qtbot.keyClick(win.search_box, Qt.Key_Down)
    assert win.result_list.currentRow() == 2
    qtbot.keyClick(win.search_box, Qt.Key_Up)
    assert win.result_list.currentRow() == 1


def test_detail_update_is_debounced_during_fast_selection(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_DETAIL_UPDATE_DELAY_MS", 20, raising=False)
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.detail_panel.show()
    monkeypatch.setattr(win.detail_panel, "isHidden", lambda: False)
    calls: list[str | None] = []
    monkeypatch.setattr(win, "_update_detail", lambda: calls.append(win._cur.name if win._cur else None))

    results = _fake_results(3)
    win._showing_recent = False
    win._results = results
    win._results_raw = results
    win._render_results(results)
    calls.clear()
    win.result_list.setCurrentRow(0)
    win.result_list.setCurrentRow(1)
    win.result_list.setCurrentRow(2)

    assert calls == []
    qtbot.waitUntil(lambda: calls == ["deck-2.pptx"], timeout=1000)


def test_detail_update_uses_restartable_timer(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_DETAIL_UPDATE_DELAY_MS", 20, raising=False)
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.detail_panel.show()
    monkeypatch.setattr(win.detail_panel, "isHidden", lambda: False)
    calls: list[int] = []
    monkeypatch.setattr(win, "_run_detail_update", lambda token: calls.append(token))

    win._schedule_detail_update()
    win._schedule_detail_update()
    win._schedule_detail_update()

    qtbot.waitUntil(lambda: bool(calls), timeout=1000)
    qtbot.wait(80)

    assert calls == [win._detail_update_token]


def test_detail_dot_refresh_is_debounced_during_fast_selection(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_DETAIL_DOT_DELAY_MS", 20, raising=False)

    class CountingVersions:
        def __init__(self):
            self.paths: list[str] = []

        def list_versions(self, path):
            self.paths.append(path)
            return [{"version_id": "v1"}]

    vm = CountingVersions()
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), version_mgr=vm, do_index=False)
    qtbot.addWidget(win)

    results = _fake_results(3)
    win._showing_recent = False
    win._results = results
    win._results_raw = results
    win._render_results(results)
    vm.paths.clear()

    win.result_list.setCurrentRow(0)
    win.result_list.setCurrentRow(1)
    win.result_list.setCurrentRow(2)

    assert vm.paths == []
    qtbot.waitUntil(lambda: vm.paths == ["C:/deck-2.pptx"], timeout=1000)


def test_detail_dot_refresh_uses_restartable_timer(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(MainWindow, "_DETAIL_DOT_DELAY_MS", 20, raising=False)
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    calls: list[int] = []
    monkeypatch.setattr(win, "_run_detail_dot_refresh", lambda token: calls.append(token))

    win._schedule_detail_dot_refresh()
    win._schedule_detail_dot_refresh()
    win._schedule_detail_dot_refresh()

    qtbot.waitUntil(lambda: bool(calls), timeout=1000)
    qtbot.wait(80)

    assert calls == [win._detail_dot_token]


def test_detail_dot_refresh_checks_versions_in_background(qtbot, monkeypatch, tmp_path):
    tasks = []
    calls = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                if value is None:
                    callback()
                else:
                    callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    class CountingVersions:
        def list_versions(self, path):
            calls.append(path)
            return [{"version_id": "v1"}]

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), version_mgr=CountingVersions(), do_index=False)
    qtbot.addWidget(win)
    win._cur = FileResult(file_id=1, path="C:/deck-a.pptx", name="deck-a.pptx", ext=".pptx",
                          mtime=0, size=1, page_count=1, status="ok", score=1, name_hit=False)
    win.detail_panel.hide()

    win._refresh_detail_dot()

    assert calls == []
    assert tasks and tasks[-1].label == "detail-dot-check"

    result = tasks[-1].fn()
    tasks[-1].done.emit(result)
    tasks[-1].finished.emit()

    assert calls == ["C:/deck-a.pptx"]
    assert not win._detail_dot.isHidden()


def test_detail_dot_refresh_reuses_inflight_check(qtbot, monkeypatch, tmp_path):
    tasks = _install_fake_background_task(monkeypatch)
    calls = []

    class CountingVersions:
        def list_versions(self, path):
            calls.append(path)
            return [{"version_id": "v1"}]

    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), version_mgr=CountingVersions(), do_index=False)
    qtbot.addWidget(win)
    win._cur = FileResult(file_id=1, path="C:/deck-a.pptx", name="deck-a.pptx", ext=".pptx",
                          mtime=0, size=1, page_count=1, status="ok", score=1, name_hit=False)
    win.detail_panel.hide()
    tasks.clear()

    win._refresh_detail_dot()
    first_task = tasks[-1]
    win._refresh_detail_dot()
    win._refresh_detail_dot()

    dot_tasks = [task for task in tasks if task.label == "detail-dot-check"]
    assert dot_tasks == [first_task]
    assert calls == []

    _finish_fake_task(first_task)

    assert calls == ["C:/deck-a.pptx"]
    assert not win._detail_dot.isHidden()


def test_detail_dot_refresh_new_token_allows_new_check(qtbot, monkeypatch, tmp_path):
    tasks = _install_fake_background_task(monkeypatch)
    calls = []

    class CountingVersions:
        def list_versions(self, path):
            calls.append(path)
            if path.endswith("deck-b.pptx"):
                return [{"version_id": "v1"}]
            return []

    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), version_mgr=CountingVersions(), do_index=False)
    qtbot.addWidget(win)
    win.detail_panel.hide()
    win._cur = FileResult(file_id=1, path="C:/deck-a.pptx", name="deck-a.pptx", ext=".pptx",
                          mtime=0, size=1, page_count=1, status="ok", score=1, name_hit=False)

    win._refresh_detail_dot()
    old_task = tasks[-1]
    win._detail_dot_token += 1
    win._cur = FileResult(file_id=2, path="C:/deck-b.pptx", name="deck-b.pptx", ext=".pptx",
                          mtime=0, size=1, page_count=1, status="ok", score=1, name_hit=False)
    win._refresh_detail_dot()
    new_task = tasks[-1]

    assert new_task is not old_task

    _finish_fake_task(new_task)
    assert not win._detail_dot.isHidden()

    _finish_fake_task(old_task)
    assert not win._detail_dot.isHidden()
    assert calls == ["C:/deck-b.pptx", "C:/deck-a.pptx"]


def test_late_detail_payload_does_not_override_newer_payload(qtbot, monkeypatch, tmp_path):
    tasks = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, value=None):
            for callback in list(self.callbacks):
                if value is None:
                    callback()
                else:
                    callback(value)

    class FakeTask:
        def __init__(self, fn, label="", parent=None):
            self.fn = fn
            self.label = label
            self.done = FakeSignal()
            self.finished = FakeSignal()

        def start(self):
            tasks.append(self)

    monkeypatch.setattr(main_window_mod, "BackgroundTask", FakeTask, raising=False)
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.detail_panel.show()
    monkeypatch.setattr(win.detail_panel, "isHidden", lambda: False)
    result = FileResult(file_id=1, path="C:/same.pptx", name="same.pptx", ext=".pptx",
                        mtime=0, size=1, page_count=1, status="ok", score=1, name_hit=False)
    win._cur = result
    updates: list[str] = []
    monkeypatch.setattr(
        win.detail_panel,
        "update_for",
        lambda _result, versions, **_kwargs: updates.append(versions[0]["version_id"]),
    )
    monkeypatch.setattr(win.detail_panel, "set_outline", lambda _titles: None)

    win._update_detail()
    old_task = tasks[-1]
    win._update_detail(force=True)
    new_task = tasks[-1]

    new_task.done.emit({"result": result, "versions": [{"version_id": "new"}], "titles": []})
    old_task.done.emit({"result": result, "versions": [{"version_id": "old"}], "titles": []})

    assert updates == ["new"]


def test_context_menu_copy_path_shows_feedback(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()

    menus = []

    class FakeMenu:
        def __init__(self, _parent=None):
            self.actions: list[tuple[str, object]] = []
            menus.append(self)

        def addAction(self, text, callback):
            self.actions.append((text, callback))

        def addSeparator(self):
            pass

        def exec(self, _pos):
            pass

    monkeypatch.setattr(main_window_mod, "QMenu", FakeMenu)
    item = win.result_list.item(0)
    win._context_menu(win.result_list.visualItemRect(item).center())

    copy_path = next(callback for text, callback in menus[-1].actions if "路径" in text)
    copy_path()

    assert "已复制完整路径" in win._toast_label.text()


def test_context_menu_actions_recheck_search_pending_when_triggered(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    menus = []

    class FakeMenu:
        def __init__(self, _parent=None):
            self.actions: list[tuple[str, object]] = []
            menus.append(self)

        def addAction(self, text, callback):
            self.actions.append((text, callback))

        def addSeparator(self):
            pass

        def exec(self, _pos):
            pass

    copied = []
    monkeypatch.setattr(main_window_mod, "QMenu", FakeMenu)
    monkeypatch.setattr(win, "_copy_text_with_toast", lambda text, message: copied.append((text, message)))
    item = win.result_list.item(0)
    win._context_menu(win.result_list.visualItemRect(item).center())
    copy_path = next(callback for text, callback in menus[-1].actions if "完整路径" in text)

    pending = PendingSearchWorker()
    win._search_worker = pending
    win.search_box.setText("new-query")
    win._do_search()
    copy_path()

    assert pending.requests
    assert copied == []
    assert "搜索还在进行" in win._toast_label.text()


def test_context_menu_blocked_while_file_operation_active(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    menus = []

    class FakeMenu:
        def __init__(self, _parent=None):
            menus.append(self)

        def addAction(self, text, callback):
            pass

        def addSeparator(self):
            pass

        def exec(self, _pos):
            pass

    monkeypatch.setattr(main_window_mod, "QMenu", FakeMenu)
    win._active_heavy_op = "open"
    item = win.result_list.item(0)
    win._context_menu(win.result_list.visualItemRect(item).center())

    assert menus == []
    assert "已有文件操作正在进行" in win._toast_label.text()


def test_context_menu_actions_recheck_file_operation_when_triggered(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    menus = []

    class FakeMenu:
        def __init__(self, _parent=None):
            self.actions: list[tuple[str, object]] = []
            menus.append(self)

        def addAction(self, text, callback):
            self.actions.append((text, callback))

        def addSeparator(self):
            pass

        def exec(self, _pos):
            pass

    copied = []
    monkeypatch.setattr(main_window_mod, "QMenu", FakeMenu)
    monkeypatch.setattr(win, "_copy_text_with_toast", lambda text, message: copied.append((text, message)))
    item = win.result_list.item(0)
    win._context_menu(win.result_list.visualItemRect(item).center())
    copy_path = next(callback for text, callback in menus[-1].actions if "完整路径" in text)

    win._active_heavy_op = "open"
    copy_path()

    assert copied == []
    assert "已有文件操作正在进行" in win._toast_label.text()


def test_context_menu_actions_ignore_qaction_checked_argument(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    menus = []

    class FakeMenu:
        def __init__(self, _parent=None):
            self.actions: list[tuple[str, object]] = []
            menus.append(self)

        def addAction(self, text, callback):
            self.actions.append((text, callback))

        def addSeparator(self):
            pass

        def exec(self, _pos):
            pass

    copied = []
    monkeypatch.setattr(main_window_mod, "QMenu", FakeMenu)
    monkeypatch.setattr(win, "_copy_text_with_toast", lambda text, message: copied.append((text, message)))
    item = win.result_list.item(0)
    win._context_menu(win.result_list.visualItemRect(item).center())
    copy_path = next(callback for text, callback in menus[-1].actions if "完整路径" in text)

    copy_path(True)

    assert copied
    assert copied[-1][0].endswith("算力方案v2.pptx")


def test_context_menu_jump_uses_open_feedback_and_gate(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    calls: list[tuple[object, object, str]] = []
    menus = []

    class FakeMenu:
        def __init__(self, _parent=None):
            self.actions: list[tuple[str, object]] = []
            menus.append(self)

        def addAction(self, text, callback):
            self.actions.append((text, callback))

        def addSeparator(self):
            pass

        def exec(self, _pos):
            pass

    def fake_run_bg(fn, on_done=None, label=""):
        calls.append((fn, on_done, label))
        if on_done is not None:
            on_done((True, False))

    monkeypatch.setattr(main_window_mod, "QMenu", FakeMenu)
    monkeypatch.setattr(win, "_run_bg", fake_run_bg)

    item = win.result_list.item(0)
    win._context_menu(win.result_list.visualItemRect(item).center())
    jump = next(callback for text, callback in menus[-1].actions if "跳到" in text)
    jump()

    assert calls
    assert calls[-1][2] == "open"
    assert calls[-1][1] is not None
    assert "未能自动跳到第" in win._toast_label.text()


def test_context_menu_open_actions_show_failure_feedback(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    menus = []

    class FakeMenu:
        def __init__(self, _parent=None):
            self.actions: list[tuple[str, object]] = []
            menus.append(self)

        def addAction(self, text, callback):
            self.actions.append((text, callback))

        def addSeparator(self):
            pass

        def exec(self, _pos):
            pass

    monkeypatch.setattr(main_window_mod, "QMenu", FakeMenu)
    monkeypatch.setattr(main_window_mod.actions, "open_file", lambda _path: False)
    monkeypatch.setattr(main_window_mod.actions, "open_folder", lambda _path: False)

    item = win.result_list.item(0)
    win._context_menu(win.result_list.visualItemRect(item).center())
    actions_by_text = {text: callback for text, callback in menus[-1].actions}

    actions_by_text["打开文件"]()
    qtbot.waitUntil(lambda: "文件已移动或删除" in win._toast_label.text(), timeout=1000)

    actions_by_text["打开所在文件夹"]()
    qtbot.waitUntil(lambda: "找不到所在文件夹" in win._toast_label.text(), timeout=1000)


def test_open_file_and_folder_use_background_gate(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    calls: list[tuple[object, object, str]] = []

    def fake_run_bg(fn, on_done=None, label=""):
        calls.append((fn, on_done, label))
        return True

    monkeypatch.setattr(win, "_run_bg", fake_run_bg)

    win._open_file_path("C:/missing.pptx")
    assert calls[-1][2] == "open"
    assert calls[-1][1] is not None
    assert "正在打开文件" in win._toast_label.text()
    calls[-1][1](False)
    assert "文件已移动或删除" in win._toast_label.text()

    win._open_folder_path("C:/missing.pptx")
    assert calls[-1][2] == "open"
    assert calls[-1][1] is not None
    assert "正在打开所在文件夹" in win._toast_label.text()
    calls[-1][1](False)
    assert "找不到所在文件夹" in win._toast_label.text()


def test_opening_powerpoint_releases_preview_session_before_shell_open(
    qtbot, monkeypatch, tmp_path
):
    events: list[object] = []

    class ReleasableRender(StubRender):
        def release_session(self, timeout_sec=0):
            events.append(("release", timeout_sec))
            return True

    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=ReleasableRender(), do_index=False)
    qtbot.addWidget(win)
    monkeypatch.setattr(
        main_window_mod.actions,
        "open_file",
        lambda path: events.append(("open", path)) or True,
    )

    def fake_run_bg(fn, on_done=None, label=""):
        result = fn()
        if on_done is not None:
            on_done(result)
        return True

    monkeypatch.setattr(win, "_run_bg", fake_run_bg)

    win._open_file_path("C:/deck.pptx")

    assert events[0][0] == "release"
    assert events[1] == ("open", "C:/deck.pptx")

    events.clear()
    monkeypatch.setattr(
        main_window_mod.actions,
        "open_at_page",
        lambda path, page: events.append(("goto", path, page)) or (True, True),
    )
    win._open_at_page_bg("C:/deck.pptx", 7)

    assert events[0][0] == "release"
    assert events[1] == ("goto", "C:/deck.pptx", 7)


def test_opening_powerpoint_waits_until_hidden_preview_process_is_gone(
    qtbot, monkeypatch, tmp_path,
):
    events = []

    class ReleasableRender(StubRender):
        def release_session(self, timeout_sec=0):
            events.append(("release", timeout_sec))
            return True

    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=ReleasableRender(), do_index=False)
    qtbot.addWidget(win)
    monkeypatch.setattr(
        main_window_mod.renderer_mod,
        "wait_for_external_open_ready",
        lambda timeout_sec=0: events.append(("safe", timeout_sec)) or True,
        raising=False,
    )
    monkeypatch.setattr(
        main_window_mod.actions,
        "open_file",
        lambda path: events.append(("open", path)) or True,
    )

    assert win._open_file_after_preview_release("C:/deck.pptx") is True
    assert [event[0] for event in events] == ["release", "safe", "open"]


def test_opening_powerpoint_never_shell_opens_into_a_headless_preview_session(
    qtbot, monkeypatch, tmp_path,
):
    class ReleasableRender(StubRender):
        def release_session(self, timeout_sec=0):
            return True

    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=ReleasableRender(), do_index=False)
    qtbot.addWidget(win)
    shell_opens = []
    monkeypatch.setattr(
        main_window_mod.renderer_mod,
        "wait_for_external_open_ready",
        lambda timeout_sec=0: False,
    )
    monkeypatch.setattr(
        main_window_mod.actions,
        "open_file",
        lambda path: shell_opens.append(path) or True,
    )

    assert win._open_file_after_preview_release("C:/deck.pptx") == "handoff_busy"
    assert shell_opens == []


def test_opening_powerpoint_stops_when_preview_worker_cannot_release_session(
    qtbot, monkeypatch, tmp_path,
):
    class BusyRender(StubRender):
        def release_session(self, timeout_sec=0):
            return False

    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=BusyRender(), do_index=False)
    qtbot.addWidget(win)
    shell_opens = []
    monkeypatch.setattr(
        main_window_mod.actions,
        "open_file",
        lambda path: shell_opens.append(path) or True,
    )

    assert win._open_file_after_preview_release("C:/deck.pptx") == "handoff_busy"
    assert shell_opens == []


def test_open_at_page_shows_immediate_feedback_when_background_starts(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    calls: list[tuple[object, object, str]] = []

    def fake_run_bg(fn, on_done=None, label=""):
        calls.append((fn, on_done, label))
        return True

    monkeypatch.setattr(win, "_run_bg", fake_run_bg)

    win._open_at_page_bg("C:/deck.pptx", 7)

    assert calls
    assert calls[-1][2] == "open"
    assert "正在打开第 7 页" in win._toast_label.text()


def test_copy_file_to_clipboard_does_not_sleep_in_ui_thread(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)

    def fail_sleep(_seconds):
        raise AssertionError("copy file must not sleep in the UI thread")

    monkeypatch.setattr(main_window_mod.time, "sleep", fail_sleep)
    monkeypatch.setattr(win, "_set_file_clipboard", lambda _path: None)
    monkeypatch.setattr(
        win,
        "_confirm_file_clipboard",
        lambda _path, _token, _remaining: win._toast("已复制文件到剪贴板"),
    )

    win._act_copy_clipboard()

    qtbot.waitUntil(lambda: bool(win._toast_label.text()), timeout=1000)
    assert any(
        token in win._toast_label.text()
        for token in ("已复制文件到剪贴板", "剪贴板暂时不可用")
    )


def test_copy_file_to_clipboard_does_not_probe_filesystem_in_ui_thread(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("昇腾")
    win._do_search()
    win.result_list.setCurrentRow(0)
    calls: list[tuple[object, object, str]] = []
    exists_before_bg = 0

    def fake_exists(_path):
        nonlocal exists_before_bg
        if not calls:
            exists_before_bg += 1
        return True

    def fake_run_bg(fn, on_done=None, label=""):
        calls.append((fn, on_done, label))
        return True

    monkeypatch.setattr(main_window_mod.os.path, "exists", fake_exists)
    monkeypatch.setattr(win, "_run_bg", fake_run_bg)
    monkeypatch.setattr(win, "_set_file_clipboard", lambda _path: None)
    monkeypatch.setattr(win, "_confirm_file_clipboard", lambda _path, _token, _remaining: None)

    win._act_copy_clipboard()

    assert exists_before_bg == 0
    assert calls
    assert calls[-1][2] == "copy-exists"
    assert calls[-1][1] is not None


def test_request_full_rescan_reports_rejected_when_indexer_running(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)

    class RunningIndexer:
        def isRunning(self):
            return True

        def stop(self):
            pass

        def wait(self, _ms):
            return True

    win._indexer = RunningIndexer()

    assert win._request_full_rescan() is False
    assert "正在扫描中" in win._toast_label.text()


def test_thumbnail_strip(qtbot, tmp_path):
    """多命中页生成对应数量的缩略图按钮，可切换命中页。"""
    conn = _index_multi(tmp_path, {"multi.pptx": ["算力 第一页", "算力 第二页", "算力 第三页"]})
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win.search_box.setText("算力")
    win._do_search()
    win.result_list.setCurrentRow(0)
    assert len(win._thumb_btns) == 3       # 命中 3 页 → 3 个缩略图按钮
    win._goto_hit(2)
    assert win._hit_idx == 2


def test_hit_page_strip_shows_overflow_count(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._cur = FileResult(
        file_id=1,
        path="C:/many-hits.pptx",
        name="many-hits.pptx",
        ext=".pptx",
        mtime=1,
        size=1,
        page_count=30,
        status="ok",
        score=1.0,
        name_hit=False,
        hits=[SearchHit(i, f"hit {i}") for i in range(1, 16)],
    )

    win._populate_thumbs()

    labels = [
        win.thumb_row.itemAt(i).widget().text()
        for i in range(win.thumb_row.count())
        if win.thumb_row.itemAt(i).widget() is not None
    ]
    assert len(win._thumb_btns) == 12
    assert "+3" in labels
    more = next(
        win.thumb_row.itemAt(i).widget()
        for i in range(win.thumb_row.count())
        if win.thumb_row.itemAt(i).widget() is not None
        and win.thumb_row.itemAt(i).widget().text() == "+3"
    )
    assert "还有 3 个命中页" in more.toolTip()
    assert "上/下命中页" in more.toolTip()
    assert not any(token in more.toolTip() for token in ("鏁", "鍚", "绱", "锛", "鈥", "鈫", "馃", "\ufffd"))


def test_theme_toggle(qtbot, tmp_path):
    conn = _index(tmp_path)
    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    win._apply_theme("cloud")
    win._toggle_theme()
    assert win._theme == "atelier"   # cloud 是列表末位，循环回绕到静白（列表首位）


def test_show_recent_loads_uncached_files_in_background(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    tasks = _install_fake_background_task(monkeypatch)
    calls = 0
    fake = _fake_results(2)

    def fake_recent_files(_conn, limit=20, **_kwargs):
        nonlocal calls
        if limit != 20:
            return []
        calls += 1
        return list(fake)

    monkeypatch.setattr(main_window_mod.db, "recent_files", fake_recent_files)

    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert calls == 0
    assert tasks and tasks[-1].label == "recent-files-load"

    _finish_fake_task(tasks[-1])
    assert calls == 1
    assert win._showing_recent is True
    assert [r.name for r in win._results] == ["deck-0.pptx", "deck-1.pptx"]

    win._show_recent()
    assert calls == 1

    win._show_recent(dashboard_force_refresh=True)
    assert calls == 1

    win._show_recent(recent_force_refresh=True)
    assert calls == 1
    assert tasks[-1].label == "recent-files-load"

    _finish_fake_task(tasks[-1])
    assert calls == 2


def test_show_recent_reuses_inflight_uncached_load(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    tasks = _install_fake_background_task(monkeypatch)
    calls = 0
    fake = _fake_results(2)

    def fake_recent_files(_conn, limit=20, **_kwargs):
        nonlocal calls
        if limit != 20:
            return []
        calls += 1
        return list(fake)

    monkeypatch.setattr(main_window_mod.db, "recent_files", fake_recent_files)

    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    first_task = tasks[-1]
    assert first_task.label == "recent-files-load"

    win._show_recent()
    win._show_recent()

    recent_tasks = [task for task in tasks if task.label == "recent-files-load"]
    assert recent_tasks == [first_task]
    assert calls == 0

    _finish_fake_task(first_task)

    assert calls == 1
    assert [r.name for r in win._results] == ["deck-0.pptx", "deck-1.pptx"]


def test_show_recent_force_reuses_inflight_uncached_load(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    tasks = _install_fake_background_task(monkeypatch)
    calls = 0
    fake = _fake_results(2)

    def fake_recent_files(_conn, limit=20, **_kwargs):
        nonlocal calls
        if limit != 20:
            return []
        calls += 1
        return list(fake)

    monkeypatch.setattr(main_window_mod.db, "recent_files", fake_recent_files)

    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    startup_task = tasks[-1]
    _finish_fake_task(startup_task)
    tasks.clear()

    win._show_recent(recent_force_refresh=True)
    first_force_task = tasks[-1]
    win._show_recent(recent_force_refresh=True)

    recent_tasks = [task for task in tasks if task.label == "recent-files-load"]
    assert recent_tasks == [first_force_task]
    assert calls == 1

    _finish_fake_task(first_force_task)

    assert calls == 2


def test_show_recent_cache_hit_clears_stale_inflight_marker(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    tasks = _install_fake_background_task(monkeypatch)
    fake = _fake_results(2)

    monkeypatch.setattr(
        main_window_mod.db,
        "recent_files",
        lambda _conn, limit=20, **_kwargs: list(fake),
    )

    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    startup_task = tasks[-1]
    assert win._recent_load_inflight_token is not None
    win._recent_cache = list(fake)
    win._recent_cache_at = time.monotonic()

    win._show_recent()

    assert win._recent_load_inflight_token is None
    assert [r.name for r in win._results] == ["deck-0.pptx", "deck-1.pptx"]

    _finish_fake_task(startup_task)

    assert win._recent_load_inflight_token is None


def test_show_dashboard_schedules_refresh_after_switch(qtbot, monkeypatch, tmp_path):
    win = MainWindow(conn=_index(tmp_path), render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    qtbot.wait(10)
    calls = []

    monkeypatch.setattr(
        win.dashboard,
        "refresh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dashboard refresh must be scheduled")),
    )
    monkeypatch.setattr(
        win.dashboard,
        "schedule_refresh",
        lambda *, force=False: calls.append((force, win._list_stack.currentWidget() is win.dashboard)),
    )

    win._show_list()
    win._show_dashboard(force_refresh=True)

    assert calls == [(True, True)]


def test_live_refresh_forces_recent_cache_refresh(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    tasks = _install_fake_background_task(monkeypatch)
    calls = 0

    def fake_recent_files(_conn, limit=20, **_kwargs):
        nonlocal calls
        if limit != 20:
            return []
        calls += 1
        return _fake_results(1)

    monkeypatch.setattr(main_window_mod.db, "recent_files", fake_recent_files)

    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    assert calls == 0
    _finish_fake_task(tasks[-1])
    assert calls == 1
    assert win._showing_recent is True

    win._do_live_refresh()

    assert calls == 1
    assert tasks[-1].label == "recent-files-load"
    _finish_fake_task(tasks[-1])
    assert calls == 2


def test_stale_recent_load_does_not_override_search_results(qtbot, monkeypatch, tmp_path):
    conn = _index(tmp_path)
    tasks = _install_fake_background_task(monkeypatch)

    monkeypatch.setattr(
        main_window_mod.db,
        "recent_files",
        lambda _conn, limit=20, **_kwargs: _fake_results(2),
    )

    win = MainWindow(conn=conn, render_worker=StubRender(), do_index=False)
    qtbot.addWidget(win)
    startup_task = tasks[-1]

    win.search_box.setText("昇腾")
    win._do_search()
    assert win.result_list.count() == 1

    _finish_fake_task(startup_task)

    assert win._showing_recent is False
    assert len(win._results) == 1
    assert win._results[0].name == "算力方案v2.pptx"
