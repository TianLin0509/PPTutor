"""Native hidden GUI acceptance, usable from the final frozen application."""
import json,time
from pathlib import Path


def main(args):
    from PySide6.QtCore import Qt,QRect
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from ..ui.screenshot_window import ScreenshotWindow
    from ..ui.screenshot_overlay import ScreenshotOverlay
    from ..native_clipboard import opened_clipboard
    import win32clipboard as clipboard,win32con
    index=args.index('--formula-ui-selftest');source=QImage(args[index+1]);root=Path(args[index+2]);root.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([]);checks={};win=ScreenshotWindow(output_root=root/'shots')
    for widget in [win,win._formula.dialog,win._notice]:widget.setAttribute(Qt.WA_DontShowOnScreen,True)
    def wait():
        deadline=time.monotonic()+120
        while win._busy:
            app.processEvents();time.sleep(.02)
            if time.monotonic()>deadline:raise TimeoutError('公式界面验收超时')
        app.processEvents()
    def copied():
        with opened_clipboard(0):return clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
    try:
        if source.isNull():raise ValueError('公式验收图片无效')
        overlay=ScreenshotOverlay(source,QRect(0,0,source.width(),source.height()),QRect(0,0,source.width(),source.height()))
        overlay.setAttribute(Qt.WA_DontShowOnScreen,True);overlay.formula_selected.connect(win._formula_selected);overlay.show()
        overlay.formula_btn.click();wait();dialog=win._formula.dialog
        checks['formula_button_runs_real_local_model']=bool(dialog.editor.toPlainText())
        checks['auto_latex_native_clipboard']=copied()==dialog.editor.toPlainText()
        checks['preview_rendered']=dialog.preview.pixmap() is not None and not dialog.preview.pixmap().isNull()
        dialog.grab().save(str(root/'formula-dialog.png'))
        dialog.word_btn.click();wait();checks['word_button_native_clipboard']=bool(dialog.word.toPlainText()) and copied()==dialog.word.toPlainText()
        recognized=dialog.editor.toPlainText()
        dialog.editor.setPlainText('x^2');checks['editing_invalidates_word']=not dialog.word_btn.isEnabled() and not dialog.word.toPlainText()
        dialog.refresh_btn.click();wait();checks['refresh_word_and_preview']=dialog.word.toPlainText()=='x^(2)' and dialog.word_btn.isEnabled()
        win.close();win.show();win._formula.show();dialog.latex_btn.click();wait()
        checks['close_reopen_can_copy']=copied()=='x^2' and dialog.latex_btn.isEnabled()
        original=win._formula.renderer
        def unavailable(*a,**kw):raise RuntimeError('受控缺浏览器验收')
        win._formula.renderer=unavailable;dialog.editor.setPlainText('x+1');dialog.refresh_btn.click();wait()
        checks['no_browser_keeps_copy_enabled']=dialog.latex_btn.isEnabled() and dialog.word_btn.isEnabled() and '预览' in dialog.status.text()
        win._formula.renderer=original
        win._formula.start(source);dialog.reject();wait()
        checks['cancel_keeps_previous_clipboard']=copied()=='x^2' and not win._busy
        result={'checks':checks,'passed':all(checks.values()),'recognized_latex':recognized}
    except Exception as exc:result={'checks':checks,'passed':False,'error':str(exc)}
    finally:win.close();app.processEvents()
    (root/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if result['passed'] else 1
