"""The test-integrity digest must scan the root it was captured under.

A baseline captured under a repo root but compared against an unset or unrelated
root reported every test file as "removed". Because ``pre_verify`` short-circuits
on ``tamper`` *before* it inspects the last exit code, that false accusation
could not be cleared by a genuinely green run — the gate refused completion
forever while no file had actually been touched.
"""
import importlib.util
import pathlib

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "__init__.py"
_SPEC = importlib.util.spec_from_file_location("hday_under_test", str(PLUGIN))
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def test_no_accusation_when_root_was_never_recorded():
    """`_root` unset (evicted/recreated record) must not mean "all deleted"."""
    rec = {"test_digest": {"tests/conftest.py": {"asserts": 3, "lines": 10}},
           "_root": None}
    assert mod._digest_regressions(rec) == []


def test_capture_records_the_root_it_scanned(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_x.py").write_text("def test_a():\n    assert 1\n")

    rec = {}
    mod._capture_test_digest(rec, str(root))

    assert rec["test_digest_root"] == str(root)
    assert "tests/test_x.py" in rec["test_digest"]
    assert mod._digest_regressions(rec) == []   # nothing changed yet


def test_real_removal_is_still_reported(tmp_path):
    """Binding the root must not silence a genuine deletion."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    victim = root / "tests" / "test_x.py"
    victim.write_text("def test_a():\n    assert 1\n")

    rec = {}
    mod._capture_test_digest(rec, str(root))
    victim.unlink()

    assert mod._digest_regressions(rec) == ["tests/test_x.py removed"]


def test_workspace_pointer_moving_away_is_not_tampering(tmp_path):
    """Legacy record: baseline exists, `_root` now points at an unrelated tree."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_x.py").write_text("x = 1\n")

    rec = {}
    mod._capture_test_digest(rec, str(repo))
    rec.pop("test_digest_root", None)          # simulate a legacy record
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    rec["_root"] = str(unrelated)

    assert mod._digest_regressions(rec) == []

def test_a_different_repo_with_tests_is_not_tampering(tmp_path):
    """Legacy record whose `_root` moved to another project that has tests/.

    Scanning that tree makes baseline keys vanish, which looks exactly like
    deleting this project's suite — the match must be proven first.
    """
    first = tmp_path / "project-a"
    (first / "tests").mkdir(parents=True)
    (first / "tests" / "test_only_here.py").write_text("x = 1\n")

    rec = {}
    mod._capture_test_digest(rec, str(first))
    rec.pop("test_digest_root", None)             # legacy: root not recorded

    second = tmp_path / "project-b"               # has tests/, different files
    (second / "tests").mkdir(parents=True)
    (second / "tests" / "conftest.py").write_text("x = 1\n")
    rec["_root"] = str(second)

    assert mod._digest_regressions(rec) == []
