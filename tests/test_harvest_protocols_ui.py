"""Guards the `protocols-ui` harvest deliverable against contract drift.

The harvest (`runs/harvest/protocols-ui.json` + `.md`) is data, not code, so its
contract is asserted by the standalone validator in `runs/harvest/`. This test
runs that validator in a subprocess and fails on any violation, so the repo's own
suite keeps the artifact honest as it is edited.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = REPO_ROOT / "runs" / "harvest" / "validate_protocols_ui.py"


def test_protocols_ui_harvest_contract() -> None:
    """The harvest satisfies its strict deliverable contract, verified by a real run."""
    assert VALIDATOR.is_file(), f"missing harvest validator: {VALIDATOR}"

    proc = subprocess.run(
        [sys.executable, str(VALIDATOR)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    assert proc.returncode == 0, (
        "protocols-ui harvest violated its contract "
        f"(exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    )
    assert "ALL CHECKS PASSED" in proc.stdout, proc.stdout
    assert "assertions run:" in proc.stdout, proc.stdout
