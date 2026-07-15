from __future__ import annotations

from pptx_finder.ui.index_activity_bar import IndexActivityBar


def test_indeterminate_bar_respects_reduced_motion(qtbot):
    bar = IndexActivityBar(motion_allowed=False)
    qtbot.addWidget(bar)
    bar.show()

    bar.setRange(0, 0)

    assert bar.is_indeterminate()
    assert not bar.animation_active()
    assert bar.minimum() == 0
    assert bar.maximum() == 0


def test_determinate_bar_keeps_progressbar_compatible_api(qtbot):
    bar = IndexActivityBar(motion_allowed=True)
    qtbot.addWidget(bar)
    bar.show()
    bar.setRange(0, 200)
    bar.setValue(50)

    assert not bar.is_indeterminate()
    assert not bar.animation_active()
    assert bar.minimum() == 0
    assert bar.maximum() == 200
    assert bar.value() == 50


def test_busy_animation_only_runs_while_visible(qtbot):
    bar = IndexActivityBar(motion_allowed=True)
    qtbot.addWidget(bar)
    bar.show()
    bar.setRange(0, 0)
    assert bar.animation_active()

    bar.hide()

    assert not bar.animation_active()
