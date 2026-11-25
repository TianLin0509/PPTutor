"""AI HUB-H 铁律（2026-11-25）：PPT Doctor 只在内源对用户发布；GitHub 上只留「预发布、不设为最新」的构建产物。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_ci():
    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("ci_release_test", ROOT / "tools/ci_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publish_marks_prerelease_and_never_latest(tmp_path, monkeypatch):
    ci = load_ci()
    out = tmp_path / "build-out"
    rel_dir = out / "release"
    rel_dir.mkdir(parents=True)
    (rel_dir / "manifest.json").write_text("{}", "utf-8")
    (out / "release-validation.json").write_text(json.dumps(
        {"passed": True, "source_sha": "abc", "version": "9.9.9", "release_directory": str(rel_dir)}), "utf-8")
    monkeypatch.setattr(ci.subprocess, "check_output", lambda *a, **k: "abc\n")
    monkeypatch.setattr(ci, "tag_notes", lambda tag: ("标题", "正文"))
    calls, state = [], {"created": False, "draft": True, "prerelease": False, "assets": []}

    class Result:
        def __init__(self, code=0, out=b""):
            self.returncode, self.stdout = code, out

    def fake_gh(*args, check=True, timeout=0):
        calls.append(args)
        if args[:2] == ("release", "view"):
            if "apiUrl" in args:
                return Result(0, json.dumps({"apiUrl": "repos/x/releases/1"}).encode())
            return Result(0 if state["created"] else 1)
        if args[:2] == ("release", "create"):
            state.update(created=True, prerelease="--prerelease" in args)
        if args[:2] == ("release", "upload"):
            path = Path(args[3])
            state["assets"].append({"name": path.name, "size": path.stat().st_size})
        if args[:2] == ("release", "edit"):
            state.update(draft=False, prerelease="--prerelease" in args)
        return Result()

    monkeypatch.setattr(ci, "gh", fake_gh)
    monkeypatch.setattr(ci, "api", lambda path: {"assets": state["assets"], "draft": state["draft"],
                                                  "prerelease": state["prerelease"]})
    monkeypatch.setattr(ci.subprocess, "check_output",
                        lambda cmd, **k: "abc\n" if "rev-parse" in cmd or "rev-list" in cmd else "")
    ci.cmd_publish(tmp_path)
    flat = [a for c in calls for a in c]
    assert "--latest" not in flat
    create = next(c for c in calls if c[:2] == ("release", "create"))
    edit = next(c for c in calls if c[:2] == ("release", "edit"))
    assert "--prerelease" in create and "--latest=false" in create
    assert "--prerelease" in edit and "--latest=false" in edit and "--draft=false" in edit
    assert state["draft"] is False and state["prerelease"] is True


def test_release_tools_never_read_or_set_github_latest():
    ci = (ROOT / "tools/ci_release.py").read_text("utf-8")
    assert '"--latest")' not in ci and "releases/latest" not in ci
    watch = (ROOT / "tools/mirror_watch.py").read_text("utf-8")
    assert "releases/latest" not in watch and "--blocks-only" in watch
