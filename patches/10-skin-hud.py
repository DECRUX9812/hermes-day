"""Lane 10 — Skin & HUD Engine (backend half).

Owns the ONE sanctioned colour block for the cockpit skins and the state
command that stores which skin is active.

  * ``SKIN_COLOR_CSS`` — sentinel-wrapped palette: three named skins
    (phosphor = CRT green, oxide = amber/red rust, current = the existing
    blue-slate cockpit) expressed ONLY as ``--hday-*`` custom properties on
    ``.hday-root``. Every colour literal in this lane lives inside the
    sentinel block below — never in component code, which is what the
    desktop gate enforces (``grep -cE '#<hex>|rgb(|rgba(|hsl(|hsla('
    panels/10-skin-hud.js`` must count 0).
  * Frontend half: ``panels/10-skin-hud.js`` — HUD layer, corner telemetry,
    scanline/vignette overlay, boot sweep, live signal bar, keymap
    cheatsheet, prefers-reduced-motion guard, and ``hdaySkinCss()``, the
    structural CSS-text builder. Pass this file's ``SKIN_COLOR_CSS`` to it
    (or splice the block into ``ensureDayStyles()``) so the tokens exist.

Contract: ``def register(ctx) -> None``, called once at plugin load.
Commands only — no hooks (``plugin.yaml`` is a shared file). Every command
body swallows, so a bug here degrades to "skin commands missing", never a
wedged plugin.

Skin values are design values, not metrics: they come from the spec's
per-skin candidate lists, and every override line carries exactly four
declarations so parallel edits never collide on a partial line. Tokens a
skin's candidate list does not cover are deliberately NOT re-declared — they
inherit the base value, so no colour outside a sanctioned list is ever
introduced for that skin.
"""

import re
import threading
import traceback

# Own module-level lock for this lane (never import the plugin's _LOCK).
SKIN_LOCK = threading.RLock()

# Persisted selection. Mirrors the frontend storage-key convention (*.v1).
STATE_KEY = "skin.v1"
DEFAULT_SKIN = "current"
SKINS = ("current", "phosphor", "oxide")

SKIN_BLURB = {
    "current": "blue-slate cockpit, preserved from C / --ui-* (auto default)",
    "phosphor": "CRT green — accent/danger overrides from the green candidate list",
    "oxide": "amber/red rust — dark oxide surfaces + amber accent list",
}

SKIN_COLOR_BEGIN = "/* == hday-skin:COLORS BEGIN == */"
SKIN_COLOR_END = "/* == hday-skin:COLORS END == */"

SKIN_COLOR_CSS = (
    SKIN_COLOR_BEGIN
    + """
/* --- base tokens: skin "current" (default). Values are the plugin's own C
       palette, so pre-existing UI and skin "current" agree exactly. */
.hday-root{--hday-canvas:#0b0c10;--hday-surface:#161922;--hday-surface-2:#1a1d26;--hday-line:#262a33;
--hday-line-hot:#333a47;--hday-ink:#f3f4f6;--hday-ink-2:#94a3b8;--hday-ink-3:#64748b;
--hday-accent:#38bdf8;--hday-ok:#34d399;--hday-warn:#f59e0b;--hday-danger:#ef4444;
--hday-slate:#8b949e;--hday-kind-input:#c084fc;--hday-glow:#38bdf8;--hday-scan-alpha:18%}

/* --- skin "current" re-declared under its attribute selector, same values:
       an explicit choice must be identical to the base, never a drift. */
.hday-root[data-hday-skin="current"]{--hday-canvas:#0b0c10;--hday-surface:#161922;--hday-surface-2:#1a1d26;--hday-line:#262a33;
--hday-line-hot:#333a47;--hday-ink:#f3f4f6;--hday-ink-2:#94a3b8;--hday-ink-3:#64748b;
--hday-accent:#38bdf8;--hday-ok:#34d399;--hday-warn:#f59e0b;--hday-danger:#ef4444;
--hday-slate:#8b949e;--hday-kind-input:#c084fc;--hday-glow:#38bdf8;--hday-scan-alpha:18%}

/* --- skin "phosphor": CRT green. Only hexes from the phosphor candidate
       list (accents + danger). Surfaces, warn, slate, ink-3 inherit base —
       re-declaring them would need a hex outside the candidate list. */
.hday-root[data-hday-skin="phosphor"]{--hday-ink:#8fffce;--hday-ink-2:#7dffbc;--hday-accent:#33ff77;--hday-glow:#33ff77;
--hday-ok:#4dff9e;--hday-danger:#ff5252;--hday-kind-input:#7dffbc;--hday-scan-alpha:30%}

/* --- skin "oxide": amber/red rust. Surfaces + accents from the oxide
       candidate list; ok/slate/ink/ink-3 inherit base. */
.hday-root[data-hday-skin="oxide"]{--hday-canvas:#140d0b;--hday-surface:#1c1210;--hday-surface-2:#241714;--hday-line:#38231c;
--hday-line-hot:#4a2f25;--hday-accent:#ff8c42;--hday-glow:#ff8c42;--hday-warn:#ffab70;
--hday-danger:#e2483f;--hday-ink-2:#ffb98a;--hday-kind-input:#ff9a5c;--hday-scan-alpha:22%}
"""
    + SKIN_COLOR_END
)

# Splice target note, appended only by the emit command (kept out of the
# sentinel block so the block stays pure CSS).
SPLICE_NOTE = (
    "/* splice target: ensureDayStyles() in desktop/plugin.js, or pass as the "
    "paletteCss argument of hdaySkinCss() from panels/10-skin-hud.js */"
)


# --------------------------------------------------------------------------
# state helpers (own lock, swallow everything)
# --------------------------------------------------------------------------
def _state_get(ctx, key, default=None):
    try:
        store = getattr(ctx, "state", None)
        getter = getattr(store, "get", None)
        if store is None or not callable(getter):
            return default
        return getter(key, default)
    except Exception:  # noqa: BLE001
        return default


def _state_set(ctx, key, value):
    with SKIN_LOCK:
        try:
            store = getattr(ctx, "state", None)
            setter = getattr(store, "set", None)
            if store is None or not callable(setter):
                return False
            setter(key, value)
            return True
        except Exception:  # noqa: BLE001
            return False


def active_skin(ctx):
    """Currently selected skin, normalised; unknown/absent -> DEFAULT_SKIN."""
    try:
        raw = _state_get(ctx, STATE_KEY, DEFAULT_SKIN)
        name = str(raw if raw is not None else DEFAULT_SKIN).strip().lower()
        return name if name in SKINS else DEFAULT_SKIN
    except Exception:  # noqa: BLE001
        return DEFAULT_SKIN


def palette_stats():
    """Real counts read out of SKIN_COLOR_CSS — never invented."""
    css = SKIN_COLOR_CSS
    return {
        "colour_literals": len(re.findall(r"#[0-9a-fA-F]{3,8}", css)),
        "token_declarations": len(re.findall(r"--hday-[a-z0-9-]+\s*:", css)),
        "skin_selectors": len(re.findall(r"\[data-hday-skin=", css)),
        "sentinels_ok": css.startswith(SKIN_COLOR_BEGIN) and css.endswith(SKIN_COLOR_END),
    }


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_day_skins(ctx, args):
    """List the three skins, which one is active, and palette block stats.

    `css` (or `palette`) delegates to the emit command for splice tooling.
    """
    try:
        if (args or "").strip().lower() in ("css", "palette"):
            return cmd_day_skin_css(ctx, args)
        current = active_skin(ctx)
        stats = palette_stats()
        out = [
            "hermes-day skins — active: %s (default: %s)" % (current, DEFAULT_SKIN),
        ]
        for name in SKINS:
            marker = "*" if name == current else " "
            out.append("  %s %-9s %s" % (marker, name, SKIN_BLURB.get(name, "")))
        out.append(
            "palette block: %d colour literals, %d --hday-* declarations, "
            "%d skin selectors, sentinels %s"
            % (
                stats["colour_literals"],
                stats["token_declarations"],
                stats["skin_selectors"],
                "ok" if stats["sentinels_ok"] else "BROKEN",
            )
        )
        out.append("set:  /day-skin <current|phosphor|oxide>   (state key %s)" % STATE_KEY)
        out.append("emit: /day-skins css  (or /day-skin-css) -> sanctioned block for splicing")
        out.append("note: skins recolor tokens, not layout.")
        return "\n".join(out)
    except Exception:  # noqa: BLE001
        return "day-skins failed:\n" + traceback.format_exc()


def cmd_day_skin(ctx, args):
    """Show or set the active skin (persisted under STATE_KEY)."""
    try:
        arg = (args or "").strip()
        if not arg:
            return "active skin: %s — /day-skin <current|phosphor|oxide>" % active_skin(ctx)
        name = arg.lower()
        if name not in SKINS:
            return "unknown skin %r — valid: %s (state unchanged)" % (arg, ", ".join(SKINS))
        if not _state_set(ctx, STATE_KEY, name):
            return "skin %s selected in memory; ctx.state has no writable store" % name
        return (
            "skin set: %s (state key %s). Base .hday-root stays %s until the "
            "desktop sets data-hday-skin on the root element."
            % (name, STATE_KEY, DEFAULT_SKIN)
        )
    except Exception:  # noqa: BLE001
        return "day-skin failed:\n" + traceback.format_exc()


def cmd_day_skin_css(ctx, args):
    """Emit the sanctioned sentinel colour block for splicing."""
    try:
        return SKIN_COLOR_CSS + "\n" + SPLICE_NOTE
    except Exception:  # noqa: BLE001
        return "day-skin-css failed:\n" + traceback.format_exc()


def register(ctx) -> None:
    """Called once at plugin load. Commands only, no hooks."""
    try:
        ctx.register_command(
            "day-skins",
            cmd_day_skins,
            description="List cockpit skins (phosphor/oxide/current), active pick, palette stats.",
            args_hint="",
        )
        ctx.register_command(
            "day-skin",
            cmd_day_skin,
            description="Show or set the active cockpit skin; persisted under skin.v1.",
            args_hint="[current|phosphor|oxide]",
        )
        ctx.register_command(
            "day-skin-css",
            cmd_day_skin_css,
            description="Print the sanctioned hday-skin COLORS sentinel block for splicing.",
            args_hint="",
        )
    except Exception:  # noqa: BLE001
        traceback.print_exc()
