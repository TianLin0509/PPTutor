from __future__ import annotations

from types import SimpleNamespace

import fixtures_gen as fx

from pptx_finder import db, indexer
from pptx_finder import path_policy
from pptx_finder.path_policy import (
    explicit_project_output_roots,
    is_project_dist_path,
    is_project_output_path,
    project_dist_root,
)
from pptx_finder.scanner import iter_ppt_files
from pptx_finder.versioning.watcher import _Handler


def _project_dist(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")
    dist = project / "dist"
    dist.mkdir()
    return project, dist


def test_project_dist_is_excluded_but_plain_dist_is_searchable(tmp_path, monkeypatch):
    _project, generated = _project_dist(tmp_path)
    generated_deck = generated / "generated.pptx"
    fx.make_pptx(generated_deck, [{"body": "generated"}])

    business = tmp_path / "business" / "dist"
    business.mkdir(parents=True)
    business_deck = business / "quarterly.pptx"
    fx.make_pptx(business_deck, [{"body": "quarterly"}])

    assert is_project_dist_path(generated_deck)
    assert project_dist_root(generated_deck) == generated
    assert not is_project_dist_path(business_deck)
    assert {p.name for p in iter_ppt_files([str(tmp_path)])} == {"quarterly.pptx"}

    monkeypatch.setenv("PPTUTOR_INCLUDE_PROJECT_DIST", "1")
    assert not is_project_dist_path(generated_deck)


def test_policy_rescan_retires_existing_project_dist_rows(tmp_path):
    project = tmp_path / "app"
    generated = project / "dist"
    generated.mkdir(parents=True)
    generated_deck = generated / "generated.pptx"
    fx.make_pptx(generated_deck, [{"body": "generated"}])
    business_deck = tmp_path / "quarterly.pptx"
    fx.make_pptx(business_deck, [{"body": "quarterly"}])

    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    indexer.update_index(conn, [str(tmp_path)], workers=1)
    assert db.get_file_by_path(conn, str(generated_deck)) is not None

    # The next policy-version scan must remove an already indexed generated
    # path even though the file itself still exists on disk.
    (project / "package.json").write_text("{}", encoding="utf-8")
    summary = indexer.update_index(conn, [str(tmp_path)], workers=1)

    assert summary["deleted"] == 1
    assert db.get_file_by_path(conn, str(generated_deck)) is None
    assert db.get_file_by_path(conn, str(business_deck)) is not None


def test_watcher_filters_all_event_types_at_project_dist_boundary(tmp_path):
    _project, generated = _project_dist(tmp_path)
    generated_deck = str(generated / "generated.pptx")
    normal_deck = str(tmp_path / "normal.pptx")
    saved, moved, removed = [], [], []
    handler = _Handler(
        saved.append,
        lambda src, dest: moved.append((src, dest)),
        roots=[str(tmp_path)],
        on_content_saved=saved.append,
        on_removed=removed.append,
    )
    try:
        handler._trigger(generated_deck)
        assert generated_deck not in handler._timers

        handler.on_deleted(SimpleNamespace(is_directory=False, src_path=generated_deck))
        assert saved == []
        assert removed == []

        # managed -> ignored: remove old search/version identity, do not bind
        # it to the generated destination.
        handler.on_moved(SimpleNamespace(
            is_directory=False,
            src_path=normal_deck,
            dest_path=generated_deck,
        ))
        assert saved == [normal_deck]
        assert removed == [normal_deck]
        assert moved == []
        assert generated_deck not in handler._timers

        # ignored -> managed: treat as a new destination, never import the old
        # generated identity.
        handler.on_moved(SimpleNamespace(
            is_directory=False,
            src_path=generated_deck,
            dest_path=normal_deck,
        ))
        assert moved == []
        assert normal_deck in handler._timers
    finally:
        handler.stop()


def test_explicit_project_dist_root_remains_watchable(tmp_path):
    _project, generated = _project_dist(tmp_path)
    deck = str(generated / "explicit.pptx")
    handler = _Handler(lambda _path: None, roots=[str(generated)])
    try:
        handler._trigger(deck)
        assert deck in handler._timers
    finally:
        handler.stop()


def test_conservative_project_output_set_excludes_outputs_but_keeps_src(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")
    excluded = set()
    for name in ("dist", "build", "out", "target", "artifacts"):
        deck = project / name / f"{name}.pptx"
        deck.parent.mkdir()
        fx.make_pptx(deck, [{"body": name}])
        assert is_project_output_path(deck)
        excluded.add(deck.name)
    source_deck = project / "src" / "source.pptx"
    source_deck.parent.mkdir()
    fx.make_pptx(source_deck, [{"body": "source stays searchable"}])
    assert not is_project_output_path(source_deck)

    ordinary = tmp_path / "business" / "artifacts" / "quarterly.pptx"
    ordinary.parent.mkdir(parents=True)
    fx.make_pptx(ordinary, [{"body": "ordinary artifacts folder"}])
    found = {p.name for p in iter_ppt_files([str(tmp_path)])}
    assert not (found & excluded)
    assert {"source.pptx", "quarterly.pptx"} <= found


def test_explicit_project_output_root_is_recursive_and_consistent_for_live_index(tmp_path):
    project = tmp_path / "app"
    output = project / "artifacts"
    nested = output / "release" / "slides"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    deck = nested / "explicit.pptx"
    fx.make_pptx(deck, [{"body": "explicit output v1"}])
    selected = explicit_project_output_roots([str(output)])

    assert selected
    assert list(iter_ppt_files([str(output)])) == [deck]
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    summary = indexer.update_index(conn, [str(output)], workers=1)
    assert summary["scanned"] == 1
    assert db.get_file_by_path(conn, str(deck)) is not None

    fx.make_pptx(deck, [{"body": "explicit output v2"}])
    assert indexer.index_single(
        conn,
        str(deck),
        explicit_output_roots=selected,
    )
    assert db.get_file_by_path(conn, str(deck)) is not None
    deck.unlink()
    assert indexer.index_single(
        conn,
        str(deck),
        explicit_output_roots=selected,
    )
    assert db.get_file_by_path(conn, str(deck)) is None


def test_positive_marker_cache_notices_marker_deletion(tmp_path, monkeypatch):
    project, generated = _project_dist(tmp_path)
    deck = generated / "generated.pptx"
    assert is_project_output_path(deck)

    (project / "package.json").unlink()
    monkeypatch.setattr(path_policy, "_MARKER_CACHE_TTL_SEC", 0.0)
    assert not is_project_output_path(deck)


def test_new_global_output_override_keeps_legacy_override_compatible(tmp_path, monkeypatch):
    _project, generated = _project_dist(tmp_path)
    deck = generated / "generated.pptx"
    monkeypatch.setenv("PPTUTOR_INCLUDE_PROJECT_OUTPUTS", "1")
    assert not is_project_output_path(deck)
