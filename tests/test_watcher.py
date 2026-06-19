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
