"""Tests for hday_gotchas — conditional GOTCHAS.md context injection (Mandate 9).

Run: cd ~/.hermes/plugins/hermes-day && python3 -m pytest tests/test_hday_gotchas.py -q
"""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hday_gotchas as hg  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """A fake repo tree with GOTCHAS.md files, some in ignorable dirs."""
    files = {
        "GOTCHAS.md": "root gotcha",
        "src/GOTCHAS.md": "src gotcha",
        "src/foo/GOTCHAS.md": "foo gotcha line1\nfoo gotcha line2",
        "src/bar/GOTCHAS.md": "bar gotcha",
        "node_modules/pkg/GOTCHAS.md": "must be skipped",
        ".git/GOTCHAS.md": "must be skipped",
        "venvs/py312/GOTCHAS.md": "must be skipped",
        "a/b/c/GOTCHAS.md": "deep gotcha",
        "README.md": "not gotchas",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return str(tmp_path)


# ---------------------------------------------------------------- discovery


def test_discovery_skips_ignored_dirs(repo):
    found = hg.discover(repo)
    assert set(found) == {"", "src", "src/foo", "src/bar", "a/b/c"}
    # ignored dirs never leak through
    for key in found:
        assert "node_modules" not in key
        assert ".git" not in key
        assert "venvs" not in key
    assert found["src/foo"] == "foo gotcha line1\nfoo gotcha line2"
    assert found[""] == "root gotcha"


def test_discovery_empty_dir(tmp_path):
    assert hg.discover(str(tmp_path)) == {}


def test_discovery_deterministic(repo):
    assert hg.discover(repo) == hg.discover(repo)


def test_discovery_missing_root(tmp_path):
    assert hg.discover(str(tmp_path / "nope")) == {}


# ---------------------------------------------------------- prefix matching


def test_prefix_matching_only_touched_dirs(repo):
    found = hg.discover(repo)
    sections = hg.sections_for(["src/foo/main.py"], found)
    dirs = [d for d, _ in sections]
    assert "src/foo" in dirs      # prefix of a context path -> activated
    assert "src/bar" not in dirs  # sibling NOT touched -> not activated
    assert "" in dirs             # repo-root GOTCHAS.md prefixes everything


def test_sections_ordered_shallowest_then_alpha(repo):
    found = hg.discover(repo)
    sections = hg.sections_for(
        ["a/b/c/deep.py", "src/bar/x.py", "src/foo/y.py", "src/foo/z.py"], found)
    dirs = [d for d, _ in sections]
    # "src" also activates: it prefixes src/bar/x.py. Shallowest -> alpha.
    assert dirs == ["", "src", "src/bar", "src/foo", "a/b/c"]


def test_prefix_requires_path_boundary(repo):
    found = hg.discover(repo)
    # "src/foo" must not match a context path that merely starts with the same
    # characters ("src/foobar")
    dirs = [d for d, _ in hg.sections_for(["src/foobar/x.py"], found)]
    assert "src/foo" not in dirs
    assert "src/bar" not in dirs
    assert "" in dirs


def test_sections_for_ignores_unknown_dirs(repo):
    found = hg.discover(repo)
    sections = hg.sections_for(["docs/whatever.md"], found)
    dirs = [d for d, _ in sections]
    assert dirs == [""]  # only the root gotchas match


def test_sections_for_empty_inputs(repo):
    found = hg.discover(repo)
    assert hg.sections_for([], found) == []
    assert hg.sections_for(["src/foo/x.py"], {}) == []


def test_sections_for_deterministic_under_input_order(repo):
    found = hg.discover(repo)
    paths = ["a/b/c/d.py", "src/bar/x.py", "src/foo/y.py", "docs/z.md", "GOTCHAS.md"]
    baseline = hg.sections_for(paths, found)
    shuffled = list(paths)
    rng = random.Random(1234)
    for _ in range(5):
        rng.shuffle(shuffled)
        assert hg.sections_for(shuffled, found) == baseline


# ------------------------------------------------------------- build + cap


def test_build_section_empty_is_none():
    assert hg.build_section([]) is None


def test_build_simple_block(repo):
    found = hg.discover(repo)
    sections = hg.sections_for(["src/foo/main.py"], found)
    block = hg.build_section(sections)
    assert block is not None
    assert "precedence: instincts > gotchas > skills" in block.splitlines()[0].lower()
    assert "foo gotcha line1" in block
    assert len(block.splitlines()) <= hg.MAX_SECTION_LINES


def test_build_enforces_60_line_cap_with_truncation_marker():
    # 10 sections x 20 lines each = way over the cap
    sections = [(f"dir{i:02d}", "\n".join(f"line {i}-{j}" for j in range(20)))
                for i in range(10)]
    block = hg.build_section(sections)
    lines = block.splitlines()
    assert len(lines) <= hg.MAX_SECTION_LINES == 60
    # never silently dropped: a truncation comment records the count
    marker = [ln for ln in lines if ln.startswith("#")]
    assert marker, "truncation must be recorded in a comment line"
    assert "truncated" in marker[0]
    n = int(marker[0].split("truncated")[1].split()[0])
    assert n >= 1
    # truncation drops the LOWEST-relevance (deepest / last) sections first:
    # the shallowest section survives, the last one does not
    assert "dir00" in block
    assert "line 9-19" not in block


def test_build_truncates_oversized_single_section():
    sections = [("only", "\n".join(f"body {j}" for j in range(500)))]
    block = hg.build_section(sections)
    lines = block.splitlines()
    assert len(lines) <= hg.MAX_SECTION_LINES
    assert any(ln.startswith("#") and "truncated" in ln for ln in lines)
    assert "body 0" in block  # kept from the front, not the tail


def test_build_determinism(repo):
    found = hg.discover(repo)
    sections = hg.sections_for(["src/foo/a.py", "a/b/c/b.py"], found)
    assert hg.build_section(sections) == hg.build_section(list(sections))
    # and through the whole pipeline twice
    again = hg.build_section(hg.sections_for(["src/foo/a.py", "a/b/c/b.py"],
                                             hg.discover(repo)))
    assert again == hg.build_section(sections)
