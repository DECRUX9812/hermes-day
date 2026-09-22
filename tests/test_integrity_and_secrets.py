"""Tests for the test-integrity digest, credential-leak detector and receipts.

These cover the two gaps the honesty guard cannot see by construction: a test
file rewritten through a route the guard does not inspect, and a credential that
lands in the transcript through ordinary tool output.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from conftest import _fire

# Built by concatenation so no realistic-looking credential literal sits in the
# repo — the detector is exercised without shipping something that looks keyed.
FAKE_API_KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
FAKE_AWS_KEY = "AKIA" + "Q" * 16
FAKE_BEARER = "Bearer " + "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW2xY4zA"
FAKE_JWT = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "dQw4w9WgXcQabcdefgh"])
FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"


# ---------------------------------------------------------------------------
# credential-leak detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload,expected", [
    (FAKE_API_KEY, "api-key"),
    (FAKE_AWS_KEY, "api-key"),
    (FAKE_BEARER, "bearer header"),
    (FAKE_JWT, "jwt"),
    (FAKE_PEM, "private-key block"),
])
def test_secret_signs_are_detected(day, payload, expected):
    module, _, _ = day
    assert expected in module._secret_signs(payload)


@pytest.mark.parametrize("clean", [
    "all tests passed",
    "sk- short",                       # too short to be a key
    "no credentials here",
    "assert x == 1",
    "Bearer ok",                       # no payload
])
def test_clean_output_is_not_flagged(day, clean):
    module, _, _ = day
    assert module._secret_signs(clean) == []


def test_alert_never_stores_the_secret_bytes(day, repo):
    """The alarm must not itself become the leak."""
    module, ctx, sid = day
    _fire(ctx, "post_tool_call", tool_name="terminal",
          args={"command": "env"}, result=f"OPENAI_API_KEY={FAKE_API_KEY}\n",
          status="ok", session_id=sid)

    rec = module._SESSIONS[sid]
    exposure = [a for a in rec["attn"] if a["kind"] == "secret_exposure"]
    assert exposure, "a key in tool output must raise an attention item"
    assert "api-key" in exposure[0]["signs"]

    blob = json.dumps(rec)
    assert FAKE_API_KEY not in blob, "the raw credential must never be persisted"
    assert "sk-A1b2" not in blob


def test_scan_covers_non_terminal_tools_too(day):
    module, ctx, sid = day
    _fire(ctx, "post_tool_call", tool_name="read_file",
          args={"path": "/tmp/x"}, result=f"line\n{FAKE_PEM}\n",
          status="ok", session_id=sid)
    rec = module._SESSIONS[sid]
    assert any(a["kind"] == "secret_exposure" for a in rec["attn"])


def test_result_text_is_bounded_and_safe(day):
    module, _, _ = day
    assert module._result_text(None) == ""
    assert module._result_text("abc") == "abc"
    assert "b" in module._result_text({"a": "b"})
    big = module._result_text("z" * (module._SECRET_SCAN_BYTES + 5000))
    assert len(big) == module._SECRET_SCAN_BYTES


# ---------------------------------------------------------------------------
# test-integrity digest
# ---------------------------------------------------------------------------


def _seed_tests(repo: Path, count: int = 6) -> Path:
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    target = tests_dir / "test_seeded.py"
    target.write_text("".join(
        f"def test_{i}():\n    assert {i} == {i}\n\n" for i in range(count)))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "tests"], check=True)
    return target


# `_ASSERT_RE` deliberately counts `def test_...` lines as well as assertion
# lines, so removing an entire test function registers as lost strength rather
# than passing unnoticed. Each seeded function therefore contributes 2.
PER_FUNC = 2


def test_digest_finds_test_files_and_counts_assertions(day, repo):
    module, _, _ = day
    _seed_tests(repo, 6)
    digest = module._digest_tests(str(repo))
    assert "tests/test_seeded.py" in digest
    assert digest["tests/test_seeded.py"]["asserts"] == 6 * PER_FUNC


def test_digest_counts_test_definitions_as_strength(day, repo):
    """Pins the semantics above: 3 functions => 3 asserts + 3 defs."""
    module, _, _ = day
    _seed_tests(repo, 3)
    entry = module._digest_tests(str(repo))["tests/test_seeded.py"]
    assert entry["asserts"] == 3 * PER_FUNC == 6


def test_deleting_whole_test_function_is_a_regression(day, repo):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    target = _seed_tests(repo, 6)
    module._capture_test_digest(rec, str(repo))
    keep = "".join(f"def test_{i}():\n    assert {i} == {i}\n\n" for i in range(5))
    target.write_text(keep)          # one function gone, five remain
    weak = module._digest_regressions(rec)
    assert weak and "assertions" in weak[0]


def test_digest_ignores_non_test_files(day, repo):
    module, _, _ = day
    module._capture_test_digest(module._SESSIONS["sess-test"], str(repo))
    assert "a.py" not in module._SESSIONS["sess-test"]["test_digest"]


def test_capture_is_one_shot(day, repo):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    _seed_tests(repo, 4)
    module._capture_test_digest(rec, str(repo))
    first = dict(rec["test_digest"])
    _seed_tests(repo, 99)
    module._capture_test_digest(rec, str(repo))
    assert rec["test_digest"] == first, "baseline must not drift"


def test_regression_detects_dropped_assertions(day, repo):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    target = _seed_tests(repo, 6)
    module._capture_test_digest(rec, str(repo))
    target.write_text("def test_0():\n    assert 0 == 0\n")
    weak = module._digest_regressions(rec)
    assert weak and "assertions" in weak[0]


def test_regression_detects_removed_file(day, repo):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    target = _seed_tests(repo, 4)
    module._capture_test_digest(rec, str(repo))
    target.unlink()
    assert any("removed" in w for w in module._digest_regressions(rec))


def test_regression_detects_truncation(day, repo):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    body = "".join(f"# padding line {i}\n" for i in range(40))
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    target = tests_dir / "test_seeded.py"
    target.write_text("def test_x():\n    assert 1 == 1\n" + body)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "t"], check=True)
    module._capture_test_digest(rec, str(repo))
    target.write_text("def test_x():\n    assert 1 == 1\n")
    assert any("truncated" in w for w in module._digest_regressions(rec))


def test_no_regression_when_tests_are_strengthened(day, repo):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    target = _seed_tests(repo, 4)
    module._capture_test_digest(rec, str(repo))
    target.write_text(target.read_text() + "def test_new():\n    assert True\n")
    assert module._digest_regressions(rec) == []


def test_digest_is_empty_without_a_root(day):
    module, _, _ = day
    assert module._digest_tests("") == {}
    assert module._digest_tests("/nonexistent/path/xyz") == {}
    assert module._digest_regressions({}) == []


# ---------------------------------------------------------------------------
# end to end: a bypassed rewrite is still caught at completion
# ---------------------------------------------------------------------------


def test_bypassed_test_rewrite_is_caught_at_completion(day, repo):
    """No tool call the guard can see — only the completion digest notices."""
    module, _, sid = day
    rec = module._SESSIONS[sid]
    target = _seed_tests(repo, 6)
    module._capture_test_digest(rec, str(repo))
    assert rec["test_digest"]["tests/test_seeded.py"]["asserts"] == 6 * PER_FUNC

    target.write_text("def test_0():\n    pass\n")   # bypasses the guard entirely

    assert module._digest_regressions(rec), "the regression must be visible"

    out = module._pre_verify(session_id=sid, changed_paths=[str(target)], attempt=0)
    assert out is not None and out.get("action") == "continue"
    assert "regressed" in out["message"]
    assert rec["tamper"], "tamper must be recorded on the session"


def test_flagged_verdict_and_receipt_after_tamper(day, repo):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    target = _seed_tests(repo, 6)
    module._capture_test_digest(rec, str(repo))
    target.write_text("def test_0():\n    pass\n")

    out = module._pre_verify(session_id=sid, changed_paths=[str(target)],
                             attempt=module._MAX_VERIFY_NUDGES)
    assert out is None
    assert rec["verdict"] == "flagged"
    assert "suite weakened" in rec["detail"]
    assert rec["receipt"]["verdict"] == "flagged"
    assert rec["receipt"]["tamper"] == 1


# ---------------------------------------------------------------------------
# evidence receipts
# ---------------------------------------------------------------------------


def test_receipt_binds_verdict_to_evidence(day, repo):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    module._append(rec["runs"], {"cmd": "pytest -q", "exit": 0, "ok": True,
                                 "ts": time.time()}, module._MAX_RUNS)
    module._settle_verdict(rec)
    assert rec["verdict"] == "verified"
    receipt = rec["receipt"]
    assert receipt["verdict"] == "verified"
    assert receipt["cmd"] == "pytest -q"
    assert receipt["exit"] == 0
    assert receipt["runs"] == 1
    assert receipt["root"] == str(repo)


def test_receipt_carries_the_tree_when_a_snapshot_exists(day, repo):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    (repo / "a.py").write_text("x = 7\n")
    with module._LOCK:
        module._snapshot(rec, sid, str(repo), "turn")
    module._append(rec["runs"], {"cmd": "pytest", "exit": 0, "ok": True,
                                 "ts": time.time()}, module._MAX_RUNS)
    module._settle_verdict(rec)
    assert rec["receipt"]["tree"] == rec["snaps"][-1]["tree"]


def test_receipt_is_exposed_through_day_evidence(day, repo):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    module._append(rec["runs"], {"cmd": "ruff check .", "exit": 0, "ok": True,
                                 "ts": time.time()}, module._MAX_RUNS)
    module._settle_verdict(rec)
    payload = json.loads(module._cmd_evidence(sid))
    session = payload["sessions"][sid]
    assert session["receipt"]["cmd"] == "ruff check ."
    assert "tamper" in session


def test_missing_verdict_receipt_still_recorded(day):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    module._settle_verdict(rec)
    assert rec["verdict"] == "missing"
    assert rec["receipt"]["cmd"] is None
