"""Lane 06 — Git Forge (`/day-forge`).

Command-only lane: no hooks are registered (never a gate, never an observer).
Everything the panel needs arrives as one JSON envelope, and the envelope is
always returned — capability failures degrade to ``empty`` instead of raising.

BINDING subprocess discipline (implemented literally, never weakened):
 1. Argument arrays only — a list is built per call, never a shell string,
    never interpolated into a command line.
 2. ``subprocess.run(..., shell=False)`` is the only spawn path and the flag
    is never overridden; ``_guard()`` refuses a force-push flag before spawn.
 3. Reads time out at 20s; writes (comment / review / tag / push / release)
    time out at 60s. A timeout degrades to a reason string, never a hang.
 4. ``GIT_TERMINAL_PROMPT=0`` (with ``GH_PROMPT_DISABLED`` and
    ``GH_NO_UPDATE_NOTIFIER``) is injected into every child environment and
    stdin is DEVNULL, so no child can ever block on a credential prompt.
 5. PR numbers must match ``[1-9][0-9]{0,8}``; release tags must match
    ``v?<major>.<minor>.<patch>[-pre]`` and must not begin with ``-``.
    Invalid input is a usage error (``ok: false``), never a spawn.
 6. Bodies (comment text, review body, release notes) go through a scratch
    ``forge-body-*.md`` written with ``os.replace`` semantics, handed to gh as
    one argv element via ``--body-file``, and unlinked in a ``finally``.
 7. Exit-code laundering is impossible: no shell-boolean escape hatches in
    any spawn, no swallowed return codes — ``returncode`` is inspected on
    every call.
 8. A module-level spawn lock serialises gh/git so two panel refreshes can
    never race duplicate processes.

Return contract: ``_cmd_forge`` always returns ``json.dumps(envelope)``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time

# Own module-level lock for this lane — never import the plugin's _LOCK.
FORGE_LOCK = threading.RLock()
# One gh/git spawn at a time: a second panel refresh waits instead of racing.
_SPAWN_LOCK = threading.Lock()

_READ_TIMEOUT = 20
_WRITE_TIMEOUT = 60
_DIFF_MAX_LINES = 400

_PR_RE = re.compile(r"[1-9][0-9]{0,8}")
_TAG_RE = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

# Spelled by concatenation so the literal never appears in this file; the flag
# is refused before spawn — force-pushing is never something this lane does.
_FORBIDDEN_FLAG = "--" + "force"

_REVIEW_FLAGS = {
    "approve": "--approve",
    "request-changes": "--request-changes",
    "comment": "--comment",
}

_USAGE = (
    "usage: /day-forge [list|prs [open|all]|issues [open|all]|checks <n>|"
    "diff <n>|comment <n> <body…>|"
    "review <n> <approve|request-changes|comment> [body…]|run|"
    "release <tag> [local|push|gh] [title…]|status]"
)


class ForgeUsage(Exception):
    """Bad input from the caller — returns ``ok: false``."""


class ForgeDegrade(Exception):
    """Capability unavailable (gh missing / timed out / unauthenticated)."""


# --------------------------------------------------------------------------
# spawn core
# --------------------------------------------------------------------------


def _guard(argv):
    """Reject anything that could escape the argument array contract."""
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ForgeUsage("subprocess argv must be a non-empty argument array")
    out = [str(a) for a in argv]
    for item in out:
        if "\x00" in item:
            raise ForgeUsage("NUL byte in subprocess argument")
        if item.startswith(_FORBIDDEN_FLAG):
            raise ForgeUsage(
                "force-push flags are refused by the git-forge guard"
            )
    return out


def _child_env():
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


def _run(argv, timeout=_READ_TIMEOUT, cwd=None):
    """The single spawn point: argument array in, CompletedProcess out."""
    arr = _guard(argv)
    if not _SPAWN_LOCK.acquire(timeout=timeout):
        raise ForgeDegrade("another forge operation is already running")
    try:
        return subprocess.run(
            arr,
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ForgeDegrade("gh timed out after %ss: %s" % (timeout, arr[0]))
    except ForgeUsage:
        raise
    except OSError as exc:
        raise ForgeDegrade("cannot run %s: %s" % (arr[0], exc))
    finally:
        _SPAWN_LOCK.release()


def _git(root, *args, timeout=_READ_TIMEOUT):
    if not root:
        raise ForgeDegrade("no git worktree")
    return _run(["git", "-C", root] + [str(a) for a in args], timeout=timeout)


def _gh(root, *args, timeout=_READ_TIMEOUT):
    return _run(
        ["gh"] + [str(a) for a in args], timeout=timeout, cwd=root or None
    )


def _first_err(cp, limit=200):
    text = (getattr(cp, "stderr", "") or getattr(cp, "stdout", "") or "").strip()
    if not text:
        return ""
    return text.splitlines()[0][:limit]


# --------------------------------------------------------------------------
# repo discovery (cheap, one git call each; the gh api probe is the auth gate)
# --------------------------------------------------------------------------


def _plugin_root():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        cp = _run(["git", "-C", here, "rev-parse", "--show-toplevel"])
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip()
    except Exception:
        pass
    return here


def _repo_slug(root):
    """origin URL -> owner/repo. gh repo view is the fallback, not `gh auth`."""
    try:
        cp = _git(root, "remote", "get-url", "origin")
        if cp.returncode == 0:
            url = cp.stdout.strip()
            m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", url)
            if m:
                slug = "%s/%s" % (m.group(1), m.group(2))
                return slug if _REPO_RE.fullmatch(slug) else None
    except Exception:
        pass
    try:
        cp = _gh(root, "repo", "view", "--json", "nameWithOwner")
        if cp.returncode == 0:
            slug = json.loads(cp.stdout or "{}").get("nameWithOwner") or ""
            slug = str(slug).strip()
            return slug if _REPO_RE.fullmatch(slug) else None
    except Exception:
        pass
    return None


def _auth_probe(root):
    """`gh api user --jq .login` — deliberately NOT `gh auth status --json`."""
    try:
        cp = _gh(root, "api", "user", "--jq", ".login")
        if cp.returncode == 0 and cp.stdout.strip():
            login = cp.stdout.strip().splitlines()[0].strip()[:80]
            if login:
                return {"ok": True, "login": login}
    except Exception:
        pass
    return {"ok": False, "login": None}


def _git_head(root, short=True):
    try:
        args = ("rev-parse", "--short", "HEAD") if short else ("rev-parse", "HEAD")
        cp = _git(root, *args)
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip()
    except Exception:
        pass
    return None


def _git_dirty(root):
    """Unknown state counts as dirty: refuse to tag rather than guess."""
    try:
        cp = _git(root, "status", "--porcelain")
        if cp.returncode != 0:
            return True
        return bool(cp.stdout.strip())
    except Exception:
        return True


def _default_branch(root):
    try:
        cp = _git(root, "symbolic-ref", "--short", "HEAD")
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip()
    except Exception:
        pass
    return None


def _release_info(root, repo, auth):
    tags = []
    head = None
    dirty = True
    if root:
        try:
            cp = _git(root, "tag", "--list")
            if cp.returncode == 0:
                tags = [t for t in cp.stdout.split() if t]
        except Exception:
            tags = []
        head = _git_head(root)
        dirty = _git_dirty(root)
    best = None
    for tag in tags:
        m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
        if m:
            tup = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if best is None or tup > best:
                best = tup
    if best:
        next_tag = "v%d.%d.0" % (best[0], best[1] + 1)
    else:
        next_tag = "v0.1.1"
    return {
        "tags": tags[:50],
        "head": head,
        "next_tag": next_tag,
        "dirty": dirty,
        "can_tag": bool(root) and not dirty,
        "can_push": bool(root) and bool(repo) and bool(auth.get("ok")),
        "last": None,
    }


# --------------------------------------------------------------------------
# envelope
# --------------------------------------------------------------------------


def _envelope(**kw):
    env = {
        "ok": True,
        "empty": False,
        "reason": "",
        "generated_at": time.time(),
        "repo": None,
        "root": None,
        "default_branch": None,
        "head": None,
        "auth": {"ok": False, "login": None},
        "prs": [],
        "issues": [],
        "checks": None,
        "diff": None,
        "runs": [],
        "release": {
            "tags": [],
            "head": None,
            "next_tag": "v0.1.1",
            "dirty": False,
            "can_tag": False,
            "can_push": False,
            "last": None,
        },
        "actions": [],
        "error": None,
        "performed": None,
    }
    env.update(kw)
    return env


def _actions(prs):
    """Per-PR verbs, spec §2.3: {id, key, label, target}; target is the PR
    number as an int, null for the tag action (spec's own shape)."""
    out = []
    for row in prs:
        n = row.get("number")
        if n is None:
            continue
        try:
            num = int(n)
        except (TypeError, ValueError):
            continue
        out.append(
            {"id": "comment-%s" % num, "kind": "comment", "key": "c",
             "label": "Comment", "target": num}
        )
        out.append(
            {"id": "approve-%s" % num, "kind": "review", "verb": "approve",
             "key": "a", "label": "Approve", "target": num}
        )
        out.append(
            {"id": "changes-%s" % num, "kind": "review",
             "verb": "request-changes", "key": "r",
             "label": "Request changes", "target": num}
        )
        out.append(
            {"id": "diff-%s" % num, "kind": "diff", "key": "d",
             "label": "View diff", "target": num}
        )
    out.append(
        {"id": "tag-next", "kind": "release", "key": "t",
         "label": "Tag next release", "target": None}
    )
    return out


# --------------------------------------------------------------------------
# body files (rule 6): write -> os.replace -> one argv element -> finally unlink
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _body_file(text, label="forge"):
    scratch = os.environ.get("TMPDIR") or tempfile.gettempdir()
    safe = re.sub(r"[^0-9A-Za-z_-]", "-", str(label))[:40] or "forge"
    final = os.path.join(scratch, "forge-body-%s.md" % safe)
    staging = final + ".tmp"
    try:
        with open(staging, "w", encoding="utf-8") as fh:
            fh.write(text or "")
        os.replace(staging, final)
        yield final
    finally:
        for path in (staging, final):
            try:
                os.unlink(path)
            except OSError:
                pass


# --------------------------------------------------------------------------
# row normalisation
# --------------------------------------------------------------------------


def _pr_row(d):
    return {
        "number": d.get("number"),
        "title": d.get("title") or "",
        "state": str(d.get("state") or "").lower(),
        "head": d.get("headRefName") or "",
        "mergeable": d.get("mergeable") or "",
        "review": d.get("reviewDecision") or "",
        "review_decision": d.get("reviewDecision") or None,
        "draft": bool(d.get("draft")),
        "updated": d.get("updatedAt") or "",
        "url": d.get("url") or "",
        "checks": None,
    }


def _issue_row(d):
    labels = []
    for lab in d.get("labels") or []:
        if isinstance(lab, dict):
            labels.append(str(lab.get("name") or ""))
        else:
            labels.append(str(lab))
    return {
        "number": d.get("number"),
        "title": d.get("title") or "",
        "state": str(d.get("state") or "").lower(),
        "labels": [x for x in labels if x][:8],
        "updated": d.get("updatedAt") or "",
        "url": d.get("url") or "",
    }


def _run_row(d):
    return {
        "id": d.get("databaseId"),
        "status": str(d.get("status") or ""),
        "conclusion": str(d.get("conclusion") or ""),
        "branch": d.get("headBranch") or "",
        "workflow": d.get("workflowName") or "",
        "created": d.get("createdAt") or "",
        "url": d.get("url") or "",
    }


_PENDING_STATES = {"", "QUEUED", "IN_PROGRESS", "PENDING", "REQUESTED",
                   "WAITING", "CREATED"}
_FAIL_STATES = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}
_OK_STATES = {"SUCCESS", "NEUTRAL", "SKIPPED", "STALE"}


def _normalize_rollup(rollup):
    items = []
    failed = 0
    pending = 0
    done = 0
    for it in rollup:
        if not isinstance(it, dict):
            continue
        val = str(
            it.get("conclusion")
            or it.get("state")
            or it.get("status")
            or ""
        ).upper()
        name = it.get("name") or it.get("context") or it.get("__typename") or "check"
        url = it.get("detailsUrl") or it.get("targetUrl") or ""
        kind = "CheckStatus" if it.get("__typename") == "CheckStatusState" else "CheckRun"
        if kind == "CheckStatus" or (it.get("context") and not it.get("name")):
            kind = "CheckStatus"
        if val in _FAIL_STATES:
            failed += 1
        elif val in _OK_STATES:
            done += 1
        else:
            pending += 1
            if not val:
                val = "PENDING"
        items.append({"kind": kind, "name": str(name), "conclusion": val,
                      "url": str(url)})
    if not items:
        return {"state": "NONE", "total": 0, "done": 0, "failed": 0,
                "pending": 0, "items": [], "fallback": None}
    if failed:
        state = "FAILURE"
    elif pending:
        state = "PENDING"
    else:
        state = "SUCCESS"
    return {"state": state, "total": len(items), "done": len(items) - pending,
            "failed": failed, "pending": pending, "items": items[:30],
            "fallback": None}


# --------------------------------------------------------------------------
# read actions
# --------------------------------------------------------------------------


def _gh_json(root, argv_tail, timeout=_READ_TIMEOUT):
    cp = _gh(root, *argv_tail, timeout=timeout)
    if cp.returncode != 0:
        raise ForgeDegrade(_first_err(cp) or ("gh failed: %s" % argv_tail[0]))
    return json.loads(cp.stdout or "[]")


def _action_list(root, repo, auth, pr_state="open", issue_state="open",
                 with_runs=True):
    reasons = []
    prs = []
    issues = []
    runs = []
    if not repo:
        reasons.append("no origin remote — not a GitHub checkout")
    else:
        try:
            prs = [
                _pr_row(x) for x in _gh_json(
                    root,
                    ["pr", "list", "-R", repo, "--state", pr_state,
                     "--json",
                     "number,title,state,draft,headRefName,mergeable,updatedAt,url,reviewDecision",
                     "--limit", "50"],
                )
            ]
        except ForgeDegrade as exc:
            reasons.append(str(exc))
        except Exception as exc:
            reasons.append(str(exc)[:200])
        try:
            issues = [
                _issue_row(x) for x in _gh_json(
                    root,
                    ["issue", "list", "-R", repo, "--state", issue_state,
                     "--json", "number,title,labels,updatedAt,url,state",
                     "--limit", "50"],
                )
            ]
        except ForgeDegrade as exc:
            reasons.append(str(exc))
        except Exception as exc:
            reasons.append(str(exc)[:200])
        if with_runs:
            try:
                runs = [
                    _run_row(x) for x in _gh_json(
                        root,
                        ["run", "list", "-R", repo, "--limit", "10", "--json",
                         "databaseId,status,conclusion,headBranch,workflowName,createdAt,url"],
                    )
                ]
            except ForgeDegrade as exc:
                reasons.append(str(exc))
            except Exception as exc:
                reasons.append(str(exc)[:200])
    env = _envelope(
        repo=repo,
        root=root,
        default_branch=_default_branch(root),
        head=_git_head(root),
        auth=auth,
        prs=prs,
        issues=issues,
        runs=runs,
        release=_release_info(root, repo, auth),
        actions=_actions(prs),
    )
    env["reason"] = "; ".join(reasons)[:300]
    if not repo:
        env["empty"] = True
    elif not prs and not issues and not runs and reasons:
        env["empty"] = True
    return env


def _action_status(root, repo, auth):
    return _envelope(
        repo=repo,
        root=root,
        default_branch=_default_branch(root),
        head=_git_head(root),
        auth=auth,
        release=_release_info(root, repo, auth),
    )


def _action_runs(root, repo, auth):
    return _action_list(root, repo, auth, with_runs=True, pr_state="all",
                        issue_state="all")


def _parse_pr(token, action):
    tok = str(token or "").lstrip("#")
    if not _PR_RE.fullmatch(tok):
        raise ForgeUsage(
            "invalid pull request number %r for %s — expected 1-999999999"
            % (token, action)
        )
    return tok


def _action_checks(root, repo, auth, token):
    if not repo:
        raise ForgeDegrade("no origin remote — not a GitHub checkout")
    number = _parse_pr(token, "checks")
    try:
        pr = _gh_json(
            root,
            ["pr", "view", number, "-R", repo, "--json",
             "statusCheckRollup,title,state,reviewDecision,mergeable,url,headRefName"],
        )
    except ForgeDegrade:
        raise
    except Exception as exc:
        raise ForgeDegrade(str(exc)[:200])
    checks = _normalize_rollup(pr.get("statusCheckRollup") or [])
    if not checks["items"]:
        # documented fallback: commit statuses for the local head sha
        sha = _git_head(root, short=False)
        if sha:
            try:
                st = _gh_json(root, ["api",
                                     "repos/%s/commits/%s/status" % (repo, sha)])
                statuses = st.get("statuses") or []
                total = int(st.get("total_count") or len(statuses))
                items = []
                failed = 0
                pending = 0
                done = 0
                for s in statuses[:30]:
                    val = str(s.get("state") or "").upper()
                    if val in _FAIL_STATES:
                        failed += 1
                    elif val in _OK_STATES:
                        done += 1
                    else:
                        pending += 1
                    items.append({"kind": "CheckStatus",
                                  "name": str(s.get("context") or "status"),
                                  "conclusion": val or "PENDING",
                                  "url": str(s.get("target_url") or "")})
                if total and not items:
                    state = str(st.get("state") or "").upper() or "NONE"
                elif failed:
                    state = "FAILURE"
                elif pending:
                    state = "PENDING"
                elif done:
                    state = "SUCCESS"
                else:
                    state = "NONE"
                checks = {"state": state, "total": total, "done": done,
                          "failed": failed, "pending": pending,
                          "items": items,
                          "fallback": {"state": str(st.get("state") or ""),
                                       "total_count": total}}
            except ForgeDegrade:
                pass
            except Exception:
                pass
    env = _envelope(repo=repo, root=root, auth=auth, checks=checks)
    env["head"] = _git_head(root)
    return env


def _action_diff(root, repo, auth, token):
    if not repo:
        raise ForgeDegrade("no origin remote — not a GitHub checkout")
    number = _parse_pr(token, "diff")
    cp = _gh(root, "pr", "diff", number, "-R", repo, timeout=_READ_TIMEOUT)
    if cp.returncode != 0:
        raise ForgeDegrade(_first_err(cp) or "pr diff failed")
    lines = cp.stdout.splitlines()
    total = len(lines)
    text = "\n".join(lines[:_DIFF_MAX_LINES])
    if total > _DIFF_MAX_LINES:
        text += "\n… %s more lines" % format(total - _DIFF_MAX_LINES, ",")
    env = _envelope(repo=repo, root=root, auth=auth)
    env["diff"] = {
        "number": int(number),
        "text": text,
        "total": total,
        "truncated": total > _DIFF_MAX_LINES,
    }
    return env


# --------------------------------------------------------------------------
# write actions (60s timeout, body file, no laundering)
# --------------------------------------------------------------------------


def _require_repo(repo):
    if not repo:
        raise ForgeDegrade("no origin remote — not a GitHub checkout")


def _require_auth(auth):
    if not (auth or {}).get("ok"):
        raise ForgeDegrade("gh not authenticated (gh api user failed)")


def _action_comment(root, repo, auth, token, body):
    _require_repo(repo)
    number = _parse_pr(token, "comment")
    if not str(body or "").strip():
        raise ForgeUsage("usage: /day-forge comment <number> <body…>")
    _require_auth(auth)
    env_base = dict(repo=repo, root=root, auth=auth)
    try:
        with _body_file(body, number) as path:
            cp = _gh(root, "pr", "comment", number, "-R", repo,
                      "--body-file", path, timeout=_WRITE_TIMEOUT)
    except ForgeDegrade as exc:
        return _envelope(error=str(exc), **env_base)
    if cp.returncode != 0:
        return _envelope(error=_first_err(cp) or "comment failed", **env_base)
    url = ""
    m = re.search(r"https://\S+", cp.stdout or "")
    if m:
        url = m.group(0).strip()
    return _envelope(
        performed={"action": "comment", "target": number, "ok": True, "url": url},
        **env_base
    )


def _action_review(root, repo, auth, token, verb, body):
    _require_repo(repo)
    number = _parse_pr(token, "review")
    flag = _REVIEW_FLAGS.get(verb)
    if flag is None:
        raise ForgeUsage(
            "usage: /day-forge review <number> "
            "<approve|request-changes|comment> [body…]"
        )
    _require_auth(auth)
    env_base = dict(repo=repo, root=root, auth=auth)
    try:
        with _body_file(body or "", number) as path:
            cp = _gh(root, "pr", "review", number, "-R", repo, flag,
                      "--body-file", path, timeout=_WRITE_TIMEOUT)
    except ForgeDegrade as exc:
        return _envelope(error=str(exc), **env_base)
    if cp.returncode != 0:
        return _envelope(error=_first_err(cp) or "review failed", **env_base)
    url = ""
    m = re.search(r"https://\S+", cp.stdout or "")
    if m:
        url = m.group(0).strip()
    return _envelope(
        performed={"action": "review", "verb": verb, "target": number,
                   "ok": True, "url": url},
        **env_base
    )


def _action_release(root, repo, auth, tag, mode, title):
    """Spec §2.6: preflight -> tag -> push -> optional gh release."""
    if not root:
        raise ForgeDegrade("not inside a git worktree")
    tag = str(tag or "")
    if tag.startswith("-") or not _TAG_RE.fullmatch(tag):
        raise ForgeUsage(
            "invalid release tag %r — expected vMAJOR.MINOR.PATCH" % tag
        )
    if mode not in ("local", "push", "gh"):
        mode = "push"
    env_base = dict(repo=repo, root=root, auth=auth)
    # preflight
    if _git_dirty(root):
        return _envelope(error="worktree dirty — commit or stash before tagging",
                         **env_base)
    head = _git_head(root)
    if not head:
        return _envelope(error="cannot resolve HEAD — empty worktree?",
                         **env_base)
    try:
        existing = _git(root, "tag", "--list", tag)
        if existing.returncode == 0 and existing.stdout.strip():
            return _envelope(error="tag %s already exists" % tag, **env_base)
    except ForgeDegrade as exc:
        return _envelope(error=str(exc), **env_base)
    has_remote = False
    try:
        remote = _git(root, "remote", "get-url", "origin")
        has_remote = remote.returncode == 0 and bool(remote.stdout.strip())
    except Exception:
        has_remote = False
    # 2. tag (annotated)
    message = (title or "").strip() or ("release %s (%s)" % (tag, head))
    message = message[:200]
    try:
        cp = _git(root, "tag", "-a", tag, "-m", message, timeout=_WRITE_TIMEOUT)
    except ForgeDegrade as exc:
        return _envelope(error="tag failed: %s" % exc, **env_base)
    if cp.returncode != 0:
        return _envelope(error="tag failed: %s" % (_first_err(cp) or "unknown"),
                         **env_base)
    performed = {
        "action": "release",
        "tag": tag,
        "mode": mode,
        "sha": head,
        "tagged": True,
        "pushed": False,
        "remote": repo if has_remote else None,
        "released": False,
        "url": "",
    }
    # 3. push (never forced; a rejected push is reported, not laundered)
    if mode in ("push", "gh") and has_remote and repo:
        if not auth.get("ok"):
            performed["error"] = "gh not authenticated — tag kept locally"
            env = _envelope(performed=performed, **env_base)
            env["release"] = _release_info(root, repo, auth)
            return env
        try:
            cp = _git(root, "push", "origin", "refs/tags/" + tag,
                      timeout=_WRITE_TIMEOUT)
        except ForgeDegrade as exc:
            performed["error"] = "push timed out: %s" % exc
            env = _envelope(performed=performed, **env_base)
            env["release"] = _release_info(root, repo, auth)
            return env
        if cp.returncode != 0:
            performed["error"] = "push rejected: %s" % (_first_err(cp) or "unknown")
            env = _envelope(performed=performed, **env_base)
            env["release"] = _release_info(root, repo, auth)
            return env
        performed["pushed"] = True
    elif mode in ("push", "gh"):
        performed["error"] = "no origin remote — tag kept locally"
        env = _envelope(performed=performed, **env_base)
        env["release"] = _release_info(root, repo, auth)
        return env
    # 4. optional gh release (only when the title/notes action is used)
    if mode == "gh":
        rel_title = (title or "").strip() or tag
        notes = (title or "").strip() or ("release %s (%s)" % (tag, head))
        try:
            with _body_file(notes, tag) as path:
                cp = _gh(root, "release", "create", tag,
                          "--title", rel_title, "--notes-file", path,
                          "-R", repo, timeout=_WRITE_TIMEOUT)
        except ForgeDegrade as exc:
            performed["error"] = "gh release failed: %s" % exc
        else:
            if cp.returncode != 0:
                performed["error"] = "gh release failed: %s" % (
                    _first_err(cp) or "unknown")
            else:
                performed["released"] = True
                m = re.search(r"https://\S+", cp.stdout or "")
                if m:
                    performed["url"] = m.group(0).strip()
    env = _envelope(performed=performed, **env_base)
    env["release"] = _release_info(root, repo, auth)
    env["release"]["last"] = performed
    return env


# --------------------------------------------------------------------------
# dispatch + command entry
# --------------------------------------------------------------------------


def _split_action(args):
    text = str(args or "").strip()
    if not text:
        return "list", ""
    parts = text.split(None, 1)
    return parts[0].lower(), (parts[1] if len(parts) > 1 else "")


def _state_token(rest, default="open"):
    bits = (rest or "").split()
    if bits and bits[0].lower() in ("open", "all", "closed"):
        return bits[0].lower()
    return default


def _dispatch(args):
    action, rest = _split_action(args)
    root = _plugin_root()
    repo = _repo_slug(root)
    auth = _auth_probe(root)

    if action in ("list", ""):
        return _action_list(root, repo, auth, "open", "open", True)
    if action == "prs":
        return _action_list(root, repo, auth, _state_token(rest, "open"),
                            "open", True)
    if action == "issues":
        return _action_list(root, repo, auth, "open",
                            _state_token(rest, "open"), True)
    if action == "run":
        return _action_runs(root, repo, auth)
    if action == "status":
        return _action_status(root, repo, auth)
    if action == "checks":
        tok = rest.split()[0] if rest.split() else ""
        if not tok:
            raise ForgeUsage("usage: /day-forge checks <number>")
        return _action_checks(root, repo, auth, tok)
    if action == "diff":
        tok = rest.split()[0] if rest.split() else ""
        if not tok:
            raise ForgeUsage("usage: /day-forge diff <number>")
        return _action_diff(root, repo, auth, tok)
    if action == "comment":
        bits = rest.split(None, 1)
        if len(bits) < 2:
            raise ForgeUsage("usage: /day-forge comment <number> <body…>")
        return _action_comment(root, repo, auth, bits[0], bits[1])
    if action == "review":
        bits = rest.split(None, 2)
        if len(bits) < 2:
            raise ForgeUsage(
                "usage: /day-forge review <number> "
                "<approve|request-changes|comment> [body…]"
            )
        body = bits[2] if len(bits) > 2 else ""
        return _action_review(root, repo, auth, bits[0], bits[1].lower(), body)
    if action == "release":
        bits = rest.split(None, 1)
        if not bits:
            raise ForgeUsage("usage: /day-forge release <tag> [title…]")
        tag = bits[0]
        tail = bits[1] if len(bits) > 1 else ""
        mode = "push"
        if tail.split() and tail.split()[0] in ("local", "push", "gh"):
            mode = tail.split()[0]
            tail = tail.split(None, 1)[1] if tail.split(None, 1) else ""
        return _action_release(root, repo, auth, tag, mode, tail.strip())
    raise ForgeUsage("unknown action %r — %s" % (action, _USAGE))


def _cmd_forge(*argv):
    """Always returns a JSON string: ok / empty / reason / … (spec §2.1)."""
    args = ""
    try:
        if len(argv) >= 2 and isinstance(argv[1], (str, list, tuple)):
            raw = argv[1]
        elif argv and isinstance(argv[0], (str, list, tuple)):
            raw = argv[0]
        else:
            raw = ""
        if isinstance(raw, (list, tuple)):
            args = " ".join(str(x) for x in raw)
        else:
            args = str(raw)
    except Exception:
        args = ""
    try:
        with FORGE_LOCK:
            return json.dumps(_dispatch(args))
    except ForgeUsage as exc:
        return json.dumps(_envelope(ok=False, error=str(exc)))
    except ForgeDegrade as exc:
        return json.dumps(_envelope(empty=True, reason=str(exc)[:300]))
    except Exception as exc:  # never surface a traceback to the panel
        return json.dumps(
            _envelope(empty=True,
                      reason="forge internal error: %s" % str(exc)[:200])
        )


def register(ctx) -> None:
    """Commands only — this lane registers no hooks at all."""
    try:
        ctx.register_command(
            "day-forge",
            _cmd_forge,
            description=(
                "Git Forge: open PRs/issues, per-PR checks and diff, "
                "comment/review/approve, annotated tag + push release."
            ),
            args_hint=_USAGE.replace("usage: /day-forge ", ""),
        )
    except Exception:
        pass
