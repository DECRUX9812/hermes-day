"""Guards the `multi-agent-sdks` harvest deliverable against contract drift.

The harvest (`runs/harvest/multi-agent-sdks.json` + `.md`) is data, not code, so
its contract is asserted by the standalone validator in `runs/harvest/`. This test
runs that validator in a subprocess and fails on any violation, so the repo's own
suite keeps the artifact honest as it is edited.

The contract checked is the one the task brief set: the strict pattern schema
(name/framework/source/what/why/build/effort/test/confidence), 18..30 patterns,
3..6 per briefed framework, every mechanism the brief named by name actually
covered, every source URL on a host that was really fetched, and unreachable
documentation recorded rather than silently dropped.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = REPO_ROOT / "runs" / "harvest" / "validate_multi_agent_sdks.py"


def test_multi_agent_sdks_harvest_contract() -> None:
    """The harvest satisfies its strict deliverable contract, verified by a real run."""
    assert VALIDATOR.is_file(), f"missing harvest validator: {VALIDATOR}"

    proc = subprocess.run(
        [sys.executable, str(VALIDATOR)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    assert proc.returncode == 0, (
        "multi-agent-sdks harvest violated its contract "
        f"(exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    )
    assert "ALL CHECKS PASSED" in proc.stdout, proc.stdout
    assert "assertions run:" in proc.stdout, proc.stdout
