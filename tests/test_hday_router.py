"""Tests for hday_router — tool-group selection (Mandate 10, Phase 3).

Two-stage ranker over a (group -> {tool: one-liner}) catalog, mirroring
typesafe_lab.router's rank_wide / rerank / suggest shape. The default judge is
deterministic and offline; a scored judge (e.g. a SystemOneClient choice) is
pluggable. Hidden is never deleted: selection only ever ranks, the caller
decides what to surface, and everything stays reachable via tool_search.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hday_router as hr  # noqa: E402


CATALOG = {
    "github": {"create_pull_request": "Create a GitHub pull request",
               "list_pull_requests": "List GitHub pull requests for review",
               "add_pr_comment": "Comment on a GitHub pull request review"},
    "spotify": {"play_track": "Play a Spotify track",
                "search_playlist": "Search Spotify playlists"},
    "terminal": {"run_command": "Run a shell command in the terminal"},
}


def test_pr_review_selects_github_not_spotify():
    pick = hr.suggest("Review my open pull request on github", CATALOG)
    assert pick.groups and pick.groups[0] == "github"
    assert "spotify" not in pick.groups


def test_music_task_selects_spotify():
    pick = hr.suggest("play some music from my playlists", CATALOG)
    assert "spotify" in pick.groups
    assert "github" not in pick.groups


def test_unrelated_task_selects_nothing_or_weak():
    pick = hr.suggest("zzzq unrelated qqqq", CATALOG)
    # no group may be invented; an empty pick is a fine answer
    assert isinstance(pick.groups, tuple)
    assert pick.reason


def test_rank_wide_is_deterministic_and_bounded():
    a = hr.rank_wide("review pull request", CATALOG)
    b = hr.rank_wide("review pull request", CATALOG)
    assert a == b
    assert {g for g, _ in a} == set(CATALOG)


def test_judge_is_pluggable():
    calls = []

    def fake_judge(task, options):
        calls.append(sorted(options))
        return {name: (0.9 if name == "spotify" else 0.05) for name in options}

    pick = hr.suggest("anything", CATALOG, judge=fake_judge)
    assert pick.groups[0] == "spotify"
    assert calls, "the supplied judge must actually be consulted"


def test_suggestion_carries_receipt():
    pick = hr.suggest("review pull request", CATALOG)
    d = pick.as_dict()
    assert d["groups"] and d["reason"]
    assert "ranked" in d or "trace" in d
