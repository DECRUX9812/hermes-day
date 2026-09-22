"""Shared fixtures for the Hermes Day test suite.

Loads the plugin source from this repo (never the deployed copy) and wires it to
a fake plugin host pointing at a disposable git repo, so the observer hooks,
guard predicates and snapshot plumbing can be driven directly.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_INIT = REPO_ROOT / "__init__.py"

# The plugin imports agent-side helpers lazily; make the Hermes install
# importable when the caller has not already put it on sys.path.
HERMES_AGENT = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent"))
if HERMES_AGENT.is_dir() and str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))


class FakeState:
    """Minimal stand-in for ctx.state."""

    def __init__(self) -> None:
        self.store: dict = {}

    def set(self, key, value) -> None:
        self.store[key] = value

    def get(self, key, default=None):
        return self.store.get(key, default)


class FakeCtx:
    """Records what register() wires up, without a live plugin host."""

    def __init__(self) -> None:
        self.state = FakeState()
        self.hooks: dict[str, list] = {}
        self.commands: dict[str, object] = {}
        self.sections: dict[str, object] = {}

    def register_hook(self, name, fn) -> None:
        self.hooks.setdefault(name, []).append(fn)

    def register_command(self, name, fn, **kwargs) -> None:
        self.commands[name] = fn

    def register_system_prompt_section(self, name, fn, position=None, max_chars=None) -> None:
        self.sections[name] = fn

    def register_tool(self, *args, **kwargs) -> None:
        raise AssertionError("hermes-day should not register tools")

    def get_config(self, key):
        return {}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A disposable git repo, so instinct promotion and snapshots have a home."""
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


@pytest.fixture()
def day(repo: Path):
    """Fresh plugin module + registered context, bound to a temp repo."""
    spec = importlib.util.spec_from_file_location("hermes_day_under_test", PLUGIN_INIT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    ctx = FakeCtx()
    module.register(ctx)

    session_id = "sess-test"
    module._SESSIONS[session_id] = module._new_rec()
    module._SESSIONS[session_id]["_root"] = str(repo)

    yield module, ctx, session_id
    module._PENDING_APPROVALS.clear()


def _fire(ctx, hook, **kwargs):
    """Invoke every callback registered for *hook* (register() wires exactly one)."""
    callbacks = ctx.hooks[hook]
    assert callbacks, f"no callback registered for {hook}"
    for callback in callbacks:
        callback(**kwargs)
