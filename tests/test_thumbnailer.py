from __future__ import annotations

import zipfile

from PySide6.QtGui import QImage, QColor

import fixtures_gen as fx

from pptx_finder import renderer, thumbnailer


def _png_bytes(path):
    img = QImage(80, 45, QImage.Format_ARGB32)
    img.fill(QColor("#2f80ed"))
    assert img.save(str(path), "PNG")
    return path.read_bytes()


def test_embedded_thumbnail_extracts_docprops_thumbnail(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnailer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    src = tmp_path / "deck.pptx"
    fx.make_pptx(src, [{"body": "cover"}])
    png = tmp_path / "thumb.png"
    with zipfile.ZipFile(src, "a") as zf:
        zf.writestr("docProps/thumbnail.png", _png_bytes(png))

    out = thumbnailer.embedded_thumbnail(str(src))

    assert out is not None
    assert out.exists()
    assert not QImage(str(out)).isNull()


def test_find_non_com_thumbnail_skips_cover_for_non_first_page(tmp_path, monkeypatch):
    """命中页>1 且无该页渲染缓存：不回退到封面(内置/Shell)缩略图——否则会显示错的页。"""
    monkeypatch.setattr(renderer, "find_cached_render", lambda *a, **k: None)
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"x")
    used: list[str] = []

    def fake_embedded(_p):
        used.append("embedded")
        return cover

    def fake_shell(*_a, **_k):
        used.append("shell")
        return cover

    monkeypatch.setattr(thumbnailer, "embedded_thumbnail", fake_embedded)
    monkeypatch.setattr(thumbnailer, "shell_thumbnail", fake_shell)

    assert thumbnailer.find_non_com_thumbnail("deck.pptx", 7, long_edge=480) is None
    assert used == []


def test_find_non_com_thumbnail_uses_cover_for_first_page(tmp_path, monkeypatch):
    """命中页=1（封面即命中页）：仍可用内置/Shell 封面缩略图。"""
    monkeypatch.setattr(renderer, "find_cached_render", lambda *a, **k: None)
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"x")
    monkeypatch.setattr(thumbnailer, "embedded_thumbnail", lambda _p: cover)
    monkeypatch.setattr(thumbnailer, "shell_thumbnail", lambda *_a, **_k: None)

    assert thumbnailer.find_non_com_thumbnail("deck.pptx", 1) == cover


def test_find_non_com_thumbnail_prefers_existing_render_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnailer, "embedded_thumbnail", lambda _path: None)
    monkeypatch.setattr(thumbnailer, "shell_thumbnail", lambda *a, **k: None)
    cached = tmp_path / "cached.png"
    cached.write_bytes(b"png")

    calls: list[tuple[str, int, int]] = []

    def fake_cached(path, page_no, cache_key=None, min_long_edge=1):
        calls.append((path, page_no, min_long_edge))
        return cached

    monkeypatch.setattr(renderer, "find_cached_render", fake_cached)
    monkeypatch.setattr(
        renderer,
        "render_page",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("COM render not allowed")),
    )

    assert thumbnailer.find_non_com_thumbnail("deck.pptx", 7, long_edge=480) == cached
    assert calls == [("deck.pptx", 7, 480)]


def test_text_page_preview_renders_requested_non_first_page_without_office(
    qapp,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(thumbnailer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    src = tmp_path / "deck.pptx"
    fx.make_pptx(src, [
        {"body": "封面"},
        {"body": "第二页的命中内容 AI SP"},
    ])
    monkeypatch.setattr(
        renderer,
        "render_page",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Office is forbidden")),
    )

    out = thumbnailer.text_page_preview(str(src), 2, long_edge=800)

    assert out is not None
    assert out.exists()
    image = QImage(str(out))
    assert not image.isNull()
    assert image.width() == 800


def test_non_com_page_preview_uses_text_fallback_for_later_pages(tmp_path, monkeypatch):
    safe = tmp_path / "page-7-safe.png"
    safe.write_bytes(b"safe")
    monkeypatch.setattr(thumbnailer, "find_non_com_thumbnail", lambda *_a, **_k: None)
    calls: list[tuple[str, int, int]] = []

    def fake_text(path, page_no, *, long_edge):
        calls.append((path, page_no, long_edge))
        return safe

    monkeypatch.setattr(thumbnailer, "text_page_preview", fake_text)

    assert thumbnailer.find_non_com_page_preview("deck.pptx", 7, long_edge=720) == safe
    assert calls == [("deck.pptx", 7, 720)]


def test_text_page_preview_explains_unparseable_or_legacy_files(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnailer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    source = tmp_path / "legacy-or-encrypted.ppt"
    source.write_bytes(b"not-an-openxml-package")

    out = thumbnailer.text_page_preview(str(source), 1, long_edge=800)

    assert out is not None
    assert out.exists()
    assert not QImage(str(out)).isNull()
