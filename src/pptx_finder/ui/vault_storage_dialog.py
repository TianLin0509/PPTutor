"""Explicit capacity/location choice; validation and migration stay off the UI."""
import logging

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
                              QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout)

from ..config import (data_dir, get_vault_max_mb, get_version_vault_dir,
                      set_vault_storage_confirmed)
from .bg_task import BackgroundTask


def supports_storage(manager):
    probe = getattr(type(manager), 'supports', None)
    if callable(probe):
        return bool(probe(manager, 'configure_storage'))
    return callable(getattr(type(manager), 'configure_storage', None))


def size_text(value):
    return f'{value/1024**3:.2f} GB' if value >= 1024**3 else f'{value/1024**2:.1f} MB'


def storage_summary(state):
    cap = size_text(state['max_bytes']) if state['max_bytes'] else '不限'
    text = f"已保存 {size_text(state['saved_bytes'])} / 上限 {cap}"
    if state.get('temporary_bytes'):
        text += f"；处理暂存 {size_text(state['temporary_bytes'])}（单独统计）"
    if state.get('blocked'):
        text += ' · 容量已满，新增留版暂停，已有历史仍可恢复'
    elif state.get('last_error'):
        text += ' · 最近一次留版未完成，悬停查看原因'
    return text


class VaultStorageDialog(QDialog):
    progress = Signal(str)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._task = None
        self._status_task = None
        self._accept_pending = False
        self._parent_bg_tasks = getattr(parent, '_bg_tasks', None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = getattr(parent, '_parent_bg_tasks', None)
        if not isinstance(self._parent_bg_tasks, list):
            self._parent_bg_tasks = None
        self.setWindowTitle('选择版本库容量与位置')
        self.setObjectName('vaultStorageDialog')
        from ..config import get_theme
        from .theme import tok
        colors = tok(get_theme())
        self.setStyleSheet(f"QDialog#vaultStorageDialog {{ background:{colors['win']}; }}")
        self.resize(660, 380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        title = QLabel('先确认版本库保存在哪里、最多占多大')
        title.setStyleSheet('font-size:18px;font-weight:700;')
        layout.addWidget(title)
        note = QLabel('容量设置立即生效。超限时清理可回收的旧版；每份文档至少保留一个恢复点。'
                      '仍无法达标时暂停新增留版，并保留已有历史。')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.current = QLabel('正在读取当前占用…')
        self.current.setWordWrap(True)
        layout.addWidget(self.current)
        row = QHBoxLayout()
        row.addWidget(QLabel('容量上限'))
        self.limit = QComboBox()
        for label, value in [('2 GB',2048),('5 GB',5120),('10 GB',10240),('20 GB',20480),('50 GB',51200),('不限',0)]:
            self.limit.addItem(label,value)
        value=get_vault_max_mb()
        if self.limit.findData(value)<0:self.limit.addItem(f'{value} MB',value)
        self.limit.setCurrentIndex(self.limit.findData(value))
        row.addWidget(self.limit,1)
        layout.addLayout(row)
        row=QHBoxLayout()
        row.addWidget(QLabel('存储位置'))
        self.path=QLineEdit(get_version_vault_dir())
        self.path.setPlaceholderText(str(data_dir()/'vault'))
        row.addWidget(self.path,1)
        self.browse=QPushButton('选择文件夹…')
        self.browse.clicked.connect(self._browse)
        row.addWidget(self.browse)
        layout.addLayout(row)
        self.migrate=QCheckBox('更换位置时，迁移已有历史（复制校验后切换）')
        self.migrate.setChecked(True)
        self.migrate.setToolTip('取消勾选则在新位置开始，旧库完整保留，之后可切回查看。')
        layout.addWidget(self.migrate)
        detail=QLabel('上限按实际已保存文件统计，包含历史内容、版本索引与库内备份；处理中的临时文件另列。'
                      '迁移位置不会让原有历史自动变小。')
        detail.setWordWrap(True)
        detail.setStyleSheet('color:#667085;font-size:12px;')
        layout.addWidget(detail)
        self.feedback=QLabel('')
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        row=QHBoxLayout();row.addStretch()
        self.cancel=QPushButton('暂不调整')
        self.cancel.clicked.connect(self.reject)
        self.apply=QPushButton('应用并继续')
        self.apply.setDefault(True)
        self.apply.clicked.connect(self._apply)
        row.addWidget(self.cancel);row.addWidget(self.apply)
        layout.addLayout(row)
        self.progress.connect(self.feedback.setText)
        self.apply.setEnabled(False)
        self._status_task=BackgroundTask(self._read_status,'vault-storage-status')
        self._status_task.done.connect(self._status_ready)
        self._status_task.finished.connect(self._status_finished)
        self._track_task(self._status_task)
        self._status_task.start()

    def _track_task(self, task):
        if self._parent_bg_tasks is not None:
            self._parent_bg_tasks.append(task)
            task.finished.connect(lambda: self._parent_bg_tasks.remove(task)
                                  if task in self._parent_bg_tasks else None)

    def _read_status(self):
        try:return {'ok':True,'state':self._manager.storage_status()}
        except Exception as exc:
            logging.getLogger(__name__).warning('storage status unavailable',exc_info=True)
            return {'ok':False,'error':str(exc)}

    def _status_ready(self,result):
        if not isinstance(result,dict) or not result.get('ok'):
            self.current.setText('当前占用无法读取：'+str((result or {}).get('error','任务未完成')))
            return
        state=result['state']
        self.current.setText(storage_summary(state))
        self.current.setToolTip(state['directory']+'\n'+str(state.get('last_error') or ''))

    def _status_finished(self):
        self._status_task=None
        self.apply.setEnabled(True)

    def _browse(self):
        path=QFileDialog.getExistingDirectory(self,'选择版本库存储位置',self.path.text() or str(data_dir()))
        if path:self.path.setText(path)

    def _apply(self):
        if self._task is not None or self._status_task is not None:return
        path, limit, migrate=self.path.text(),int(self.limit.currentData()),self.migrate.isChecked()
        for widget in (self.apply,self.cancel,self.path,self.limit,self.browse,self.migrate):widget.setEnabled(False)
        self.feedback.setText('正在检查并应用设置；通常数秒，迁移大量历史可能需要数分钟…')
        def configure():
            try:
                state=self._manager.configure_storage(path,limit,migrate=migrate,
                    progress_cb=lambda copied,total:self.progress.emit(f'正在迁移已有历史：{copied}/{total} 个文件…'))
                set_vault_storage_confirmed()
                return {'ok':True,'state':state}
            except Exception as exc:
                logging.getLogger(__name__).error('storage configuration failed',exc_info=True)
                return {'ok':False,'error':str(exc)}
        self._task=BackgroundTask(configure,'vault-storage-configure')
        self._task.done.connect(self._configured)
        self._task.finished.connect(self._configured_finished)
        self._track_task(self._task)
        self._task.start()

    def _configured(self,result):
        if not isinstance(result,dict) or not result.get('ok'):
            self.feedback.setText('未完成：'+str((result or {}).get('error','任务未完成')))
            return
        self.current.setText(storage_summary(result['state']))
        self.current.setToolTip(result['state']['directory']+'\n'+str(result['state'].get('last_error') or ''))
        if result['state'].get('blocked'):
            self.feedback.setText('设置已应用，但现有数据尚未降到上限。请选择更大的容量；'
                                '也可关闭此窗口查看已有历史，新版本不会继续堆积。')
            self.cancel.setText('保留设置并关闭')
            self._accepted_with_overflow=True
        else:
            # End exec() only after the worker has emitted finished. Otherwise
            # its QThread or connected widgets could be destroyed too early.
            self._accept_pending=True

    def _configured_finished(self):
        self._task=None
        for widget in (self.apply,self.cancel,self.path,self.limit,self.browse,self.migrate):widget.setEnabled(True)
        if self._accept_pending:
            self._accept_pending=False
            self.accept()

    def reject(self):
        if self._task is not None or self._status_task is not None:
            self.feedback.setText('正在检查或应用设置，完成后可关闭。')
            return
        if getattr(self,'_accepted_with_overflow',False):self.accept()
        else:super().reject()
