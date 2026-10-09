"""Storage choices are explicit and do not initialize lazy backends on the UI."""
from types import SimpleNamespace
import threading

import pytest
from PySide6.QtWidgets import QDialog

from pptx_finder import config
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.settings_dialog import SettingsDialog


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('PPTX_FINDER_DATA_DIR', str(tmp_path/'data'))


class Manager:
    def configure_storage(self, *args, **kwargs):
        raise AssertionError('Backend must only be resolved by a background worker')


def test_first_open_prompts_once_and_cancel_still_allows_history(monkeypatch):
    import pptx_finder.ui.vault_storage_dialog as module
    opened=[]
    class Dialog:
        def __init__(self, manager, parent):opened.append(manager)
        def exec(self):return QDialog.Rejected
    monkeypatch.setattr(module,'VaultStorageDialog',Dialog)
    owner=SimpleNamespace(_version_mgr=Manager())
    MainWindow._prompt_version_storage(owner)
    MainWindow._prompt_version_storage(owner)
    assert opened==[owner._version_mgr]
    assert not config.get_vault_storage_confirmed()


def test_confirmed_storage_is_not_prompted_again(monkeypatch):
    import pptx_finder.ui.vault_storage_dialog as module
    config.set_vault_storage_confirmed()
    monkeypatch.setattr(module,'VaultStorageDialog',lambda *a:pytest.fail('Already confirmed'))
    MainWindow._prompt_version_storage(SimpleNamespace(_version_mgr=Manager()))


def test_cancelled_enable_does_not_turn_on_watcher(monkeypatch):
    import pptx_finder.ui.vault_storage_dialog as module
    config.set_version_management_enabled(False)
    class Dialog:
        def __init__(self,*args):pass
        def exec(self):return QDialog.Rejected
    monkeypatch.setattr(module,'VaultStorageDialog',Dialog)
    control=SimpleNamespace(setChecked=lambda value:None)
    owner=SimpleNamespace(_mgr=Manager(), version_feature=control,
                          _sync_feature_controls=lambda:None,
                          apply_runtime_feature_state=lambda *a:None,
                          _on_feature_change=lambda *a:pytest.fail('Cancelled enable'))
    SettingsDialog._toggle_feature(owner,'version_management',True)
    assert not config.get_version_management_enabled()


def test_dialog_resolves_lazy_backend_in_worker_and_finishes_before_accept(qtbot):
    from pptx_finder.app import _LazyVersionManager
    from pptx_finder.ui.vault_storage_dialog import VaultStorageDialog
    ui_thread=threading.get_ident()
    called=[]
    state={'saved_bytes':100,'temporary_bytes':0,'max_bytes':10240*1024**2,
           'directory':'D:/isolated-vault','blocked':False,'last_error':''}
    class Backend:
        def storage_status(self):
            called.append(('status',threading.get_ident()))
            return state
        def configure_storage(self,*args,**kwargs):
            called.append(('configure',threading.get_ident()))
            return state
    def factory():
        called.append(('factory',threading.get_ident()))
        return Backend()
    dlg=VaultStorageDialog(_LazyVersionManager(factory))
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda:dlg._status_task is None)
    dlg._apply()
    qtbot.waitUntil(lambda:dlg._task is None and dlg.result()==QDialog.Accepted)
    assert all(thread!=ui_thread for _,thread in called)
    assert config.get_vault_storage_confirmed()


def test_dialog_failure_does_not_claim_confirmation_and_remains_retryable(qtbot):
    from pptx_finder.ui.vault_storage_dialog import VaultStorageDialog
    class Backend:
        def storage_status(self):raise PermissionError('size unavailable')
        def configure_storage(self,*args,**kwargs):raise OSError('destination denied')
    dlg=VaultStorageDialog(Backend())
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda:dlg._status_task is None)
    assert 'size unavailable' in dlg.current.text()
    dlg._apply()
    qtbot.waitUntil(lambda:dlg._task is None)
    assert 'destination denied' in dlg.feedback.text()
    assert dlg.apply.isEnabled()
    assert not config.get_vault_storage_confirmed()


def test_dialog_overflow_shows_pause_and_keeps_existing_history_access(qtbot):
    from pptx_finder.ui.vault_storage_dialog import VaultStorageDialog
    state={'saved_bytes':19.9*1024**3,'temporary_bytes':0,'max_bytes':10*1024**3,
           'directory':'D:/isolated-vault','blocked':True,'last_error':'capacity'}
    class Backend:
        def storage_status(self):return state
        def configure_storage(self,*args,**kwargs):return state
    dlg=VaultStorageDialog(Backend())
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda:dlg._status_task is None)
    dlg._apply()
    qtbot.waitUntil(lambda:dlg._task is None)
    assert '暂停' in dlg.current.text()
    assert '19.90 GB' in dlg.current.text()
    assert '10.00 GB' in dlg.current.text()
    assert '尚未降到上限' in dlg.feedback.text()
    assert config.get_vault_storage_confirmed()
    dlg.reject()
    assert QDialog.result(dlg)==QDialog.Accepted


def test_capacity_failure_keeps_search_fresh_without_snapshot_retries():
    from pptx_finder.app import _FeatureRuntime
    from pptx_finder.versioning.vault import VaultCapacityError
    seen=[]
    calls=[]
    class Backend:
        def snapshot_now(self,path):
            calls.append(path)
            raise VaultCapacityError('容量已满')
    bridge=SimpleNamespace(emit_content_changed=seen.append)
    runtime=_FeatureRuntime(SimpleNamespace(),Backend(),bridge)
    runtime.version_enabled=True
    notices=[]
    runtime._report_runtime_error=notices.append
    runtime._on_ppt_saved('deck.pptx')
    runtime._on_ppt_saved('deck.pptx')
    assert calls==['deck.pptx','deck.pptx']
    assert seen==['deck.pptx','deck.pptx']
    assert notices==['容量已满']
    assert '容量' in runtime._version_error


def test_force_quit_defers_until_storage_transaction_finishes(qtbot,monkeypatch,tmp_path):
    from PySide6.QtWidgets import QApplication
    from pptx_finder import db
    from pptx_finder.ui.bg_task import BackgroundTask
    conn=db.connect(tmp_path/'index.db')
    window=MainWindow(conn=conn,do_index=False,roots=[])
    qtbot.addWidget(window)
    quit_called=[]
    monkeypatch.setattr(QApplication,'quit',lambda:quit_called.append(True))
    started,finish=threading.Event(),threading.Event()
    def transaction():
        started.set()
        assert finish.wait(5)
    task=BackgroundTask(transaction,'vault-storage-configure')
    window._bg_tasks.append(task)
    task.finished.connect(lambda:window._bg_tasks.remove(task))
    task.start()
    qtbot.waitUntil(started.is_set)
    try:
        window.force_quit()
        assert quit_called==[]
        assert not window._closing
        assert task.isRunning()
    finally:
        finish.set()
        qtbot.waitUntil(lambda:window._closing)
        qtbot.waitUntil(lambda:bool(quit_called))
        conn.close()


def test_tray_exit_action_uses_safe_quit_and_hides_tray_only_after_transaction(qtbot,monkeypatch,tmp_path):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication
    from pptx_finder import db, app as app_mod
    from pptx_finder.ui.bg_task import BackgroundTask
    class App(QObject):
        aboutToQuit=Signal()
        def quit(self):
            quit_called.append(True)
            self.aboutToQuit.emit()
    app=App()
    conn=db.connect(tmp_path/'index.db')
    window=MainWindow(conn=conn,do_index=False,roots=[])
    qtbot.addWidget(window)
    quit_called=[]
    tray_hidden=[]
    tray=SimpleNamespace(hide=lambda:tray_hidden.append(True))
    action=QAction('退出',window)
    app_mod._connect_tray_quit(app,window,tray,action)
    monkeypatch.setattr(QApplication,'quit',app.quit)
    started,finish=threading.Event(),threading.Event()
    task=BackgroundTask(lambda:(started.set(),finish.wait(5)),'vault-storage-configure')
    window._bg_tasks.append(task)
    task.finished.connect(lambda:window._bg_tasks.remove(task))
    task.start()
    qtbot.waitUntil(started.is_set)
    try:
        action.trigger()
        assert quit_called==tray_hidden==[]
        assert not window._closing
    finally:
        finish.set()
        qtbot.waitUntil(lambda:window._closing)
        qtbot.waitUntil(lambda:bool(quit_called))
        conn.close()
    assert tray_hidden==[True]
