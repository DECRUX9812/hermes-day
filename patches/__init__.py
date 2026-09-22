"""Auto-discovered patch modules for hermes-day.

Each feature lane owns exactly one file in this directory, so parallel builders
never edit `__init__.py` together — the classic parallel-build corruption mode.

Contract for a patch module:
    def register(ctx) -> None: ...   # called once, plugin load time

Rules (from BRIEF.md):
  * register COMMANDS only. New hooks would each require a manifest edit in
    `plugin.yaml`, which is a shared file — one lane at a time for that.
  * start every hook-like body with try/except; a broken patch must never take
    the plugin down, and `pre_tool_call` must stay the ONLY fail-closed gate.
  * mutate shared state only under the `threading.RLock` the main module holds.

A patch that raises is skipped with a warning, never propagated: the plugin
degrades to "that feature is missing", not "the cockpit is dead".
"""

import importlib.util
import pathlib
import traceback

__all__ = ["register_all"]

_PATCH_DIR = pathlib.Path(__file__).resolve().parent


def register_all(ctx) -> dict:
    """Load every `patches/NN-*.py`, calling its `register(ctx)`.

    Returns {module_name: "ok" | "skipped" | "error:<Type>"} so the caller can
    surface which lanes actually came up.
    """
    results = {}
    for path in sorted(_PATCH_DIR.glob("[0-9][0-9]-*.py")):
        name = path.stem
        try:
            spec = importlib.util.spec_from_file_location(f"hday_patch_{name}", path)
            if spec is None or spec.loader is None:
                results[name] = "skipped"
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:          # import/definition failure
            results[name] = f"error:{type(exc).__name__}"
            traceback.print_exc()
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            results[name] = "skipped"     # no register() = nothing to wire
            continue
        try:
            register(ctx)
            results[name] = "ok"
        except Exception as exc:          # wiring failure — isolate it
            results[name] = f"error:{type(exc).__name__}"
            traceback.print_exc()
    return results
