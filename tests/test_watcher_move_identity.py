from __future__ import annotations

from pptx_finder.versioning.watcher import _Handler


class _MoveEvent:
    is_directory = False
    src_path = r"C:\Users\me\Desktop\before.pptx"
    dest_path = r"C:\Users\me\Desktop\after.pptx"


def test_handler_reports_move_source_and_still_debounces_destination():
    moved = []
    saved = []
    handler = _Handler(saved.append, lambda src, dest: moved.append((src, dest)))

    handler.on_moved(_MoveEvent())

    assert moved == [(_MoveEvent.src_path, _MoveEvent.dest_path)]
    assert _MoveEvent.dest_path in handler._timers
    for timer in handler._timers.values():
        timer.cancel()
