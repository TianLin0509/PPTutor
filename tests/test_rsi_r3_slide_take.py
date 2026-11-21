"""RSI 第三轮：带走当前预览页；真实 Office 测试只使用无窗口临时文稿。"""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QInputDialog, QMenu

from pptx_finder import ppt_transfer as transfer
from pptx_finder.models import FileResult
from pptx_finder.ui import main_window as module
from test_ppt_transfer import app_with_decks, deck
from test_ui import StubRender, _index_multi


def test_imports_are_from_this_worktree():
    assert Path(transfer.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])


def test_copy_passes_preview_page_to_com_and_opens_only_snapshot(monkeypatch, tmp_path):
    source = tmp_path / "original.ppt"
    source.write_bytes(b"original deck")
    copied, snapshots = [], []

    @contextmanager
    def session(*, require_open=False):
        assert require_open

        def open_snapshot(path):
            assert path != source and path.suffix == source.suffix
            assert path.read_bytes() == source.read_bytes()
            snapshots.append(path)
            return SimpleNamespace(Slides=SimpleNamespace(
                Count=3, Item=lambda page: SimpleNamespace(Copy=lambda: copied.append(page))))

        yield SimpleNamespace(open=open_snapshot)

    monkeypatch.setattr(transfer, "temporary_presentations", session)
    monkeypatch.setattr(transfer, "flush_clipboard", lambda: None)
    transfer.copy_slide(source, page=2)
    assert copied == [2]
    assert source.read_bytes() == b"original deck"
    assert not snapshots[0].exists()


def test_append_passes_page_to_insert_and_reads_snapshot(monkeypatch, tmp_path):
    source = tmp_path / "original.pptx"
    source.write_bytes(b"source deck")
    target_deck = deck()
    insert = target_deck.Slides.InsertFromFile
    snapshots = []

    def checked_insert(path, *args):
        snapshot = Path(path)
        assert snapshot != source and snapshot.read_bytes() == b"source deck"
        snapshots.append(snapshot)
        return insert(path, *args)

    target_deck.Slides.InsertFromFile = checked_insert
    app_with_decks(monkeypatch, [target_deck])
    assert transfer.append_slide(source, transfer.list_presentations()[0], page=2) == 4
    assert target_deck.calls[0][1:] == (3, 2, 2)
    assert len(target_deck.calls) == 1
    assert source.read_bytes() == b"source deck" and not snapshots[0].exists()


@pytest.mark.parametrize("page", [0, -1, 1.5, True])
def test_invalid_page_is_rejected_before_com(page):
    with pytest.raises(transfer.TransferError, match="页码"):
        transfer.copy_slide("must-not-open.pptx", page=page)
    with pytest.raises(transfer.TransferError, match="页码"):
        transfer.append_slide("must-not-open.pptx", None, page=page)


@pytest.fixture
def preview(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))
    win = module.MainWindow(
        conn=_index_multi(tmp_path, {"source.pptx": ["命中首页", "第二页", "第三页"]}),
        render_worker=StubRender(), do_index=False, index_all_files_enabled=False)
    qtbot.addWidget(win)
    win.resize(1440, 900)
    win.show()
    win.search_box.setText("命中")
    win._do_search()
    assert win._cur
    win._act_goto_page(2)
    assert win._view_page == 2 and win._cur.hits[0].page_no == 1
    yield win
    qtbot.waitUntil(lambda: not win._bg_tasks, timeout=15000)


def test_buttons_follow_selection_and_non_ppt_results(preview):
    win = preview
    assert win.copy_slide_btn.isVisible() and win.copy_slide_btn.isEnabled()
    assert win.insert_slide_btn.isVisible() and win.insert_slide_btn.isEnabled()
    assert win.goto_btn.objectName() == "primary"
    assert win.clip_btn.text() == "复制文件" and "整个文件" in win.clip_btn.toolTip()
    assert win.more_btn.menu().actions() == [win.folder_btn, win.clip_btn]
    assert win.more_btn.menu().toolTipsVisible()
    for ext in (".docx", ".pdf", ".txt"):
        win._cur = FileResult(file_id=2, path=f"C:/document{ext}", name=f"document{ext}", ext=ext,
                              mtime=0, size=1, page_count=3, status="ok", score=1, name_hit=True)
        win._set_ops_enabled(True)
        assert win.copy_slide_btn.isHidden() and win.insert_slide_btn.isHidden()
        assert not win.copy_slide_btn.isEnabled() and win.open_btn.isEnabled()
    win._cur = None
    win._set_ops_enabled(False)
    assert win.copy_slide_btn.isHidden() and not win.more_btn.isEnabled()


def test_copy_is_background_busy_and_uses_current_preview_page(preview, qtbot, monkeypatch):
    win = preview
    entered, release = threading.Event(), threading.Event()
    calls = []
    ui_thread = QThread.currentThread()

    def copy(source, page=1):
        calls.append((source, page, QThread.currentThread() != ui_thread))
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(transfer, "copy_slide", copy)
    qtbot.mouseClick(win.copy_slide_btn, Qt.LeftButton)
    try:
        qtbot.waitUntil(entered.is_set)
        assert not win.copy_slide_btn.isEnabled() and not win.insert_slide_btn.isEnabled()
        assert not win.goto_btn.isEnabled() and "复制中" in win.copy_slide_btn.text()
        win._act_copy_slide()  # Programmatic/double invocation is also gated.
        assert len(calls) == 1
    finally:
        release.set()
    qtbot.waitUntil(lambda: not win._bg_tasks)
    assert calls == [(win._cur.path, 2, True)]
    assert win.copy_slide_btn.isEnabled()
    assert win._toast_label.text() == "已复制第 2 页，到 PPT 左侧缩略图区 Ctrl+V 粘贴"


def test_insert_chooses_exact_target_in_background_and_keeps_captured_page(preview, qtbot, monkeypatch):
    win = preview
    targets = [transfer.PresentationTarget("汇报.pptx", f"C:/{i}/汇报.pptx", f"window-{i}", 7)
               for i in range(2)]
    ui_thread = QThread.currentThread()
    calls = []

    def list_targets():
        assert QThread.currentThread() != ui_thread
        return targets

    def choose(*args):
        assert QThread.currentThread() == ui_thread
        assert targets[1].full_name in args[3][1]
        win._view_page = 3  # Navigation cannot change the page already chosen for transfer.
        return args[3][1], True

    def append(source, target, page=1):
        assert QThread.currentThread() != ui_thread
        calls.append((source, target, page))
        return 8

    monkeypatch.setattr(transfer, "list_presentations", list_targets)
    monkeypatch.setattr(QInputDialog, "getItem", choose)
    monkeypatch.setattr(transfer, "append_slide", append)
    qtbot.mouseClick(win.insert_slide_btn, Qt.LeftButton)
    qtbot.waitUntil(lambda: bool(calls) and not win._bg_tasks)
    assert calls == [(win._cur.path, targets[1], 2)]
    assert win._toast_label.text() == "已插入到《汇报.pptx》第 8 页"
    assert win.insert_slide_btn.isEnabled()


@pytest.mark.parametrize("error,expected", [
    (transfer.TransferError("请先在 Microsoft PowerPoint 中打开目标 PPT，再重试。"), "打开目标 PPT"),
    (PermissionError("locked"), "被占用"),
    (FileNotFoundError("missing"), "移动或删除"),
    (RuntimeError("PowerPoint 未安装"), "已安装 Microsoft PowerPoint"),
])
def test_copy_failure_reports_reason_and_next_step(preview, qtbot, monkeypatch, error, expected):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(transfer, "copy_slide", fail)
    preview.copy_slide_btn.click()
    qtbot.waitUntil(lambda: not preview._bg_tasks)
    assert "失败" in preview._toast_label.text() and expected in preview._toast_label.text()
    assert preview.copy_slide_btn.isEnabled()


def test_empty_targets_and_cancel_do_not_insert(preview, qtbot, monkeypatch):
    monkeypatch.setattr(transfer, "list_presentations", lambda: [])
    preview.insert_slide_btn.click()
    qtbot.waitUntil(lambda: not preview._bg_tasks)
    assert "打开目标文稿" in preview._toast_label.text()
    target = transfer.PresentationTarget("target", "target", "target", 1)
    monkeypatch.setattr(transfer, "list_presentations", lambda: [target])
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args: ("", False))
    monkeypatch.setattr(transfer, "append_slide", lambda *a, **k: pytest.fail("cancelled insertion"))
    preview.insert_slide_btn.click()
    qtbot.waitUntil(lambda: not preview._bg_tasks)
    assert preview.insert_slide_btn.isEnabled()


def test_uncertain_insertion_reports_no_retry(preview, qtbot, monkeypatch):
    target = transfer.PresentationTarget("target", "target", "target", 1)
    calls = []
    monkeypatch.setattr(transfer, "list_presentations", lambda: [target])
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a: (a[3][0], True))

    def uncertain(*args, **kwargs):
        calls.append(kwargs)
        raise transfer.TransferError("插入未能确认完成，请先检查目标 PPT，避免重复插入。")

    monkeypatch.setattr(transfer, "append_slide", uncertain)
    preview.insert_slide_btn.click()
    qtbot.waitUntil(lambda: bool(calls) and not preview._bg_tasks)
    assert calls == [{"page": 2}]
    assert "避免重复插入" in preview._toast_label.text()
    assert preview.insert_slide_btn.isEnabled()


def test_context_actions_use_preview_page_and_keep_existing_actions(preview, qtbot, monkeypatch):
    menus, copies = [], []

    class NonModalMenu(QMenu):
        def exec(self, *args):
            menus.append(self)

    monkeypatch.setattr(module, "QMenu", NonModalMenu)
    monkeypatch.setattr(transfer, "copy_slide", lambda source, page=1: copies.append((source, page)))
    item = preview.result_list.currentItem()
    preview._context_menu(preview.result_list.visualItemRect(item).center())
    actions = {a.text(): a for a in menus[-1].actions()}
    assert {"复制本页幻灯片", "插入到打开的 PPT…", "打开文件", "打开所在文件夹", "复制文件名"} <= actions.keys()
    actions["复制本页幻灯片"].trigger()
    qtbot.waitUntil(lambda: not preview._bg_tasks)
    assert copies == [(preview._cur.path, 2)]
    preview._cur.ext = ".docx"
    preview._context_menu(preview.result_list.visualItemRect(item).center())
    assert not any("本页" in a.text() or "插入到" in a.text() for a in menus[-1].actions())


def test_more_actions_keep_folder_and_whole_file_copy(preview, monkeypatch):
    calls = []
    monkeypatch.setattr(preview, "_open_folder_path", lambda p: calls.append(("folder", p)))
    monkeypatch.setattr(preview, "_set_file_clipboard", lambda p: calls.append(("file", p)))
    monkeypatch.setattr(preview, "_check_clipboard_file_exists_bg", lambda *a: None)
    monkeypatch.setattr(preview, "_confirm_file_clipboard", lambda *a: None)
    preview.folder_btn.trigger()
    preview.clip_btn.trigger()
    assert calls == [("folder", preview._cur.path), ("file", preview._cur.path)]


def test_action_row_at_desktop_width(preview, qtbot):
    preview.resize(1440, 900)
    qtbot.wait(50)
    buttons = [preview.goto_btn, preview.open_btn, preview.copy_slide_btn,
               preview.insert_slide_btn, preview.more_btn]
    assert preview.width() == 1440
    for left, right in zip(buttons, buttons[1:]):
        assert left.geometry().right() < right.geometry().left()
    for button in buttons:
        needed = button.fontMetrics().horizontalAdvance(button.text()) + 12
        if not button.icon().isNull():
            needed += button.iconSize().width() + 4
        assert button.width() >= needed, (button.text(), button.width(), needed)
    dest = Path(__file__).resolve().parents[1] / "artifacts" / "rsi-r3-preview-1440.png"
    dest.parent.mkdir(exist_ok=True)
    assert preview.grab().save(str(dest))


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "win32", reason="需要 Microsoft PowerPoint COM")
def test_real_powerpoint_copy_and_insert_second_slide_without_windows(tmp_path, monkeypatch):
    """Only activation/visible-target discovery is substituted; copy/clipboard/insert are real."""
    import win32clipboard
    from pptx import Presentation
    from pptx_finder import renderer

    source = tmp_path / "three-pages.pptx"
    ppt = Presentation()
    for i in range(1, 4):
        slide = ppt.slides.add_slide(ppt.slide_layouts[6])
        slide.shapes.add_textbox(0, 0, 1000000, 1000000).text = f"PAGE-{i}"
    ppt.save(source)
    original = source.read_bytes()

    # An unavailable installation/unsafe activation may skip; assertions after activation must fail.
    context = transfer.temporary_presentations()
    try:
        session = context.__enter__()
    except (transfer.TransferError, renderer.PowerPointSessionBusy) as exc:
        pytest.skip(f"PowerPoint 无法安全启动：{exc}")
    except Exception as exc:
        import pywintypes
        if isinstance(exc, pywintypes.com_error):
            pytest.skip(f"PowerPoint COM 不可用：{exc}")
        raise
    try:
        target = session.new()
        assert int(target.Windows.Count) == 0
        before = int(target.Slides.Count)
        target.Slides.Add(before + 1, 12)  # ppLayoutBlank

        @contextmanager
        def hidden_copy_session(*, require_open=False):
            # Production requires an open user deck. For this test use only our hidden deck.
            assert require_open
            try:
                yield session
            finally:
                for pres in list(session.owned):
                    if pres is not target:
                        session.close(pres)

        monkeypatch.setattr(transfer, "temporary_presentations", hidden_copy_session)
        transfer.copy_slide(source, page=2)
        formats = []
        win32clipboard.OpenClipboard()
        try:
            fmt = 0
            while True:
                fmt = win32clipboard.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                if fmt >= 0xC000:
                    formats.append(win32clipboard.GetClipboardFormatName(fmt))
        finally:
            win32clipboard.CloseClipboard()
        assert any(name.startswith("PowerPoint") and "slide" in name.lower() for name in formats), formats

        # Targets intentionally exclude hidden decks; exercise the same insertion helper directly.
        with transfer._source_snapshot(source) as snapshot:
            added = transfer._insert_slide(target, snapshot, page=2)
        assert added == before + 2 and int(target.Slides.Count) == added
        shapes = target.Slides.Item(added).Shapes
        text = "\n".join(str(shapes.Item(i).TextFrame.TextRange.Text)
                         for i in range(1, int(shapes.Count) + 1) if shapes.Item(i).HasTextFrame)
        assert text == "PAGE-2"
        assert int(target.Windows.Count) == 0
        assert source.read_bytes() == original
    finally:
        # Audited cleanup closes session.owned only; it never quits a borrowed application.
        context.__exit__(None, None, None)
