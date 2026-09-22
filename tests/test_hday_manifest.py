"""Tests for hday_manifest — one shared repo-state walk for N background tasks.

Run: cd ~/.hermes/plugins/hermes-day && python3 -m pytest tests/test_hday_manifest.py -q
"""

import hashlib
import json
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import hday_manifest as hm  # noqa: E402


A_PY = "def foo():\n    return 1\n\nclass Bar:\n    def method(self):\n        pass\n"
B_PY = "x = 1\n"
NOTES = "hello\nworld\n"
SUB_PY = "def baz():\n    pass\n"


@pytest.fixture
def repo(tmp_path):
    """A small tree with source files plus noise dirs that must be skipped."""
    (tmp_path / "a.py").write_text(A_PY)
    (tmp_path / "b.py").write_text(B_PY)
    (tmp_path / "notes.txt").write_text(NOTES)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text(SUB_PY)
    # Noise dirs at top level and nested.
    for noise in ("node_modules", "venv", ".venv", "__pycache__", ".git"):
        d = tmp_path / noise
        d.mkdir()
        (d / "junk.py").write_text("def should_not_appear():\n    pass\n")
    nested = tmp_path / "sub" / "__pycache__"
    nested.mkdir()
    (nested / "c.cpython-311.pyc").write_bytes(b"\x00\x01binary")
    return tmp_path


def test_build_manifest_shape_and_loc(repo):
    m = hm.build_manifest(repo)
    assert set(m) == {"root", "built_at", "files", "symbols", "recent_diff", "total_loc"}
    assert m["root"] == str(repo.resolve()) or m["root"] == os.path.abspath(str(repo))
    # a.py=6 lines, b.py=1, notes.txt=2, sub/c.py=2 -> noise dirs excluded
    assert m["total_loc"] == 6 + 1 + 2 + 2
    by_path = {f["path"]: f for f in m["files"]}
    assert set(by_path) == {"a.py", "b.py", "notes.txt", "sub/c.py"}
    for entry in m["files"]:
        assert set(entry) == {"path", "loc", "mtime", "sha1_10"}
        assert len(entry["sha1_10"]) == 10
        real = os.path.join(str(repo), entry["path"])
        assert abs(entry["mtime"] - os.path.getmtime(real)) < 0.05
        data = pathlib.Path(real).read_bytes()
        assert entry["sha1_10"] == hashlib.sha1(data).hexdigest()[:10]
    assert isinstance(m["recent_diff"], list)


def test_loc_counts_and_sha(repo):
    by_path = {f["path"]: f for f in hm.build_manifest(repo)["files"]}
    assert by_path["a.py"]["loc"] == len(A_PY.splitlines())
    assert by_path["b.py"]["loc"] == 1
    assert by_path["notes.txt"]["loc"] == 2
    # no trailing newline still counts its final line
    (repo / "dangling.py").write_text("one\ntwo")
    m = hm.build_manifest(repo)
    assert {f["path"]: f["loc"] for f in m["files"]}["dangling.py"] == 2
    # empty file -> 0 loc, no symbols entry
    (repo / "empty.py").write_text("")
    m2 = hm.build_manifest(repo)
    assert {f["path"]: f["loc"] for f in m2["files"]}["empty.py"] == 0


def test_symbol_extraction(repo):
    m = hm.build_manifest(repo)
    assert m["symbols"]["a.py"] == ["foo", "Bar", "method"]
    assert m["symbols"]["sub/c.py"] == ["baz"]
    # no def/class -> absent from symbols, not an empty-list entry
    assert "b.py" not in m["symbols"]
    assert "notes.txt" not in m["symbols"]
    # noise dirs never contribute symbols
    assert not any("should_not_appear" in names for names in m["symbols"].values())


def test_skip_dirs(repo):
    m = hm.build_manifest(repo)
    for f in m["files"]:
        parts = f["path"].split("/")
        assert not any(p in hm.SKIP_DIRS for p in parts), f["path"]
    # .git dir exists in fixture but must not be walked
    assert not any(f["path"].startswith(".git/") for f in m["files"])


def test_max_files_respected(repo):
    for i in range(30):
        (repo / f"f{i:02d}.py").write_text("def f():\n    pass\n")
    m = hm.build_manifest(repo, max_files=5)
    assert len(m["files"]) == 5
    m_all = hm.build_manifest(repo, max_files=2000)
    # fixture's 4 visible files + the 30 added here; noise dirs never counted
    assert len(m_all["files"]) == 34
    m_zero = hm.build_manifest(repo, max_files=0)
    assert m_zero["files"] == [] and m_zero["total_loc"] == 0


def test_not_a_repo_recent_diff_empty(tmp_path):
    (tmp_path / "x.py").write_text("def x():\n    pass\n")
    m = hm.build_manifest(tmp_path)
    assert m["recent_diff"] == []
    assert isinstance(m["recent_diff"], list)


def test_recent_diff_empty_when_git_missing(repo, monkeypatch):
    (repo / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr(hm.shutil, "which", lambda name: None)
    assert hm.build_manifest(repo)["recent_diff"] == []


def test_recent_diff_parsed_from_git(repo, monkeypatch):
    (repo / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr(hm.shutil, "which", lambda name: "/usr/bin/git")
    log_out = (
        "h1\tAnn\t2026-01-01T00:00:00+00:00\tfirst commit\n"
        "h2\tBob\t2026-01-02T00:00:00+00:00\tsecond\tnote-with-tab\n"
    )

    def fake_run(cmd, **kw):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=str(repo.resolve()), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=log_out, stderr="")

    monkeypatch.setattr(hm.subprocess, "run", fake_run)
    m = hm.build_manifest(repo)
    assert m["recent_diff"] == [
        {"hash": "h1", "author": "Ann", "date": "2026-01-01T00:00:00+00:00", "subject": "first commit"},
        {"hash": "h2", "author": "Bob", "date": "2026-01-02T00:00:00+00:00", "subject": "second\tnote-with-tab"},
    ]


def test_persist_load_roundtrip(repo, tmp_path):
    m = hm.build_manifest(repo)
    path = tmp_path / "manifest.json"
    assert hm.persist(m, str(path)) is True
    loaded, reason = hm.load(str(path))
    assert reason is None
    assert loaded["root"] == m["root"]
    assert loaded["files"] == m["files"]
    assert loaded["symbols"] == m["symbols"]
    assert loaded["total_loc"] == m["total_loc"]
    assert loaded["built_at"] == m["built_at"]
    # file on disk is plain JSON
    assert json.loads(path.read_text())["total_loc"] == m["total_loc"]


def test_persist_path_only_builds_manifest(repo, tmp_path):
    path = repo / "manifest.json"
    assert hm.persist(str(path)) is True
    loaded, reason = hm.load(str(path))
    assert reason is None
    assert loaded["root"] == str(repo.resolve()) or loaded["root"] == os.path.abspath(str(repo))


def test_load_stale_returns_none_with_reason(repo, tmp_path):
    m = hm.build_manifest(repo)
    m["built_at"] = 1000.0  # epoch -> far in the past
    path = tmp_path / "old.json"
    assert hm.persist(m, str(path)) is True
    loaded, reason = hm.load(str(path), stale_after_s=60)
    assert loaded is None
    assert reason == "stale"
    # fresh manifest still loads under the same policy
    fresh = tmp_path / "fresh.json"
    hm.persist(hm.build_manifest(repo), str(fresh))
    ok, why = hm.load(str(fresh), stale_after_s=3600)
    assert why is None and ok is not None


def test_load_missing_file(tmp_path):
    loaded, reason = hm.load(str(tmp_path / "nope.json"))
    assert loaded is None
    assert reason == "missing"


def test_load_malformed_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    loaded, reason = hm.load(str(bad))
    assert loaded is None
    assert reason == "malformed"
    # valid JSON but wrong shape
    bad.write_text(json.dumps({"nope": 1}))
    loaded, reason = hm.load(str(bad))
    assert loaded is None
    assert reason == "malformed"


def test_import_has_no_side_effects():
    # module-level state is pure data: no manifest built, nothing written
    assert callable(hm.build_manifest)
    assert callable(hm.persist)
    assert callable(hm.load)
    assert ".git" in hm.SKIP_DIRS and "node_modules" in hm.SKIP_DIRS
