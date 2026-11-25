"""scripts/automerge.py の単体テスト（GitHub には接続しない。`finish` は gh を差し替えて通す）。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import automerge as am  # noqa: E402


class EnvResolutionTest(unittest.TestCase):
    def test_parse_env_file(self):
        env = am.parse_env_file("# comment\nexport A=1\nB = 'two words'\nC=\"x\"\nD=true  # trailing\nnot a line\n")
        self.assertEqual(env, {"A": "1", "B": "two words", "C": "x", "D": "true"})

    def test_values(self):
        for raw, expected in (
            ("true", True),
            ("TRUE", True),
            (" 1 ", True),
            ("false", False),
            ("yes", False),
            (None, False),
        ):
            self.assertEqual(am.is_auto_pilot_value(raw), expected, raw)

    def test_resolution_order(self):
        r = am.resolve_auto_pilot({am.KEY: "false"}, {am.KEY: "true"}, {am.KEY: "true"})
        self.assertEqual((r.enabled, r.source), (False, "process-env"))
        r = am.resolve_auto_pilot({}, {am.KEY: "1"}, {am.KEY: "false"})
        self.assertEqual((r.enabled, r.source), (True, ".env"))
        r = am.resolve_auto_pilot({}, None, {am.KEY: "true"})
        self.assertEqual((r.enabled, r.source), (True, ".env.example"))
        r = am.resolve_auto_pilot({am.KEY: ""}, {}, {})
        self.assertEqual((r.enabled, r.source), (False, "unset"))

    def test_repository_default_is_enabled(self):
        """このリポジトリの既定（.env.example）は Auto-Pilot 有効。"""
        with tempfile.TemporaryDirectory() as t:
            # .env が無い状態での解決（手元の .env に左右されないよう .env.example だけを写す）
            Path(t, ".env.example").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
            resolution, policy = am.current_policy(env={}, root=Path(t))
        self.assertTrue(resolution.enabled)
        self.assertEqual(resolution.source, ".env.example")
        self.assertEqual(policy.after_pr, "auto-merge")
        self.assertFalse(policy.pr_draft)

    def test_policy(self):
        p = am.derive_policy(False, True)
        self.assertEqual(
            (p.plan_gate, p.pr_draft, p.after_pr, p.github_transport),
            ("await-user-approval", True, "manual", "rest-via-proxy"),
        )


def pull(**kw):
    base = dict(state="open", merged=False, draft=False, mergeable_state="clean", head_sha="abc", head_ref="feat/1-x")
    base.update(kw)
    return am.PullSnapshot(**base)


class ReadinessTest(unittest.TestCase):
    def test_ready(self):
        r = am.evaluate_merge_readiness(
            pull(), [am.CheckRun("ci", "completed", "success"), am.CheckRun("lint", "completed", "skipped")]
        )
        self.assertEqual(r.state, "ready")

    def test_failed_check_blocks(self):
        r = am.evaluate_merge_readiness(
            pull(), [am.CheckRun("ci", "completed", "failure"), am.CheckRun("x", "in_progress", None)]
        )
        self.assertEqual(r.state, "blocked")
        self.assertIn("check failed: ci (failure)", r.reasons)

    def test_running_check_waits(self):
        r = am.evaluate_merge_readiness(pull(mergeable_state="unknown"), [am.CheckRun("ci", "queued", None)])
        self.assertEqual(r.state, "pending")
        self.assertEqual(len(r.reasons), 2)

    def test_conflict_and_closed(self):
        self.assertEqual(am.evaluate_merge_readiness(pull(mergeable_state="dirty"), []).state, "blocked")
        self.assertEqual(am.evaluate_merge_readiness(pull(state="closed"), []).state, "blocked")
        self.assertEqual(am.evaluate_merge_readiness(pull(merged=True), []).reasons, ["already merged"])

    def test_no_check_run_yet_waits(self):
        """push 直後で check run が 0 件なのは「CI 通過」ではなく「まだ始まっていない」。"""
        r = am.evaluate_merge_readiness(pull(), [])
        self.assertEqual(r.state, "pending")
        self.assertIn("no check run reported for this head yet", r.reasons)

    def test_behind(self):
        r = am.evaluate_merge_readiness(pull(mergeable_state="behind"), [am.CheckRun("ci", "completed", "success")])
        self.assertEqual(r.state, "behind")

    def test_reviews_block(self):
        ok = [am.CheckRun("ci", "completed", "success")]
        states = am.latest_review_states(
            [
                {"user": {"login": "a"}, "state": "CHANGES_REQUESTED"},
                {"user": {"login": "b"}, "state": "CHANGES_REQUESTED"},
                {"user": {"login": "b"}, "state": "APPROVED"},  # b は後で承認した
                {"user": {"login": "c"}, "state": "COMMENTED"},  # コメントは判断に数えない
            ]
        )
        self.assertEqual(states, {"a": "CHANGES_REQUESTED", "b": "APPROVED"})
        r = am.evaluate_merge_readiness(pull(), ok, states)
        self.assertEqual((r.state, r.reasons), ("blocked", ["changes requested by a"]))
        r = am.evaluate_merge_readiness(pull(), ok, {}, unresolved_threads=2)
        self.assertEqual((r.state, r.reasons), ("blocked", ["2 unresolved review thread(s)"]))
        self.assertEqual(am.evaluate_merge_readiness(pull(), ok, {}, None).state, "ready")

    def test_fork_detection(self):
        self.assertFalse(pull(head_repo="o/r", base_repo="o/r").from_fork)
        self.assertTrue(pull(head_repo="someone/r", base_repo="o/r").from_fork)
        self.assertTrue(pull(head_repo="(deleted fork)", base_repo="o/r").from_fork)

    def test_self_approval(self):
        self.assertTrue(am.is_self_approval_rejection("HTTP 422: Can not approve your own pull request"))
        self.assertFalse(am.is_self_approval_rejection("HTTP 403: forbidden"))


class FakeGitHub:
    """`gh api` の代わり。PR の状態を持ち、呼ばれた操作を記録する。"""

    def __init__(self, **pr):
        self.pr = dict(
            state="open",
            merged=False,
            draft=False,
            mergeable_state="clean",
            head={"sha": "h1", "ref": "fix/1-x", "repo": {"full_name": "o/r"}},
            base={"ref": "main", "repo": {"full_name": "o/r"}},
        )
        self.pr.update(pr)
        self.checks = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        self.reviews: list[dict] = []
        self.calls: list[tuple] = []
        self.approve_error: str | None = "HTTP 422: Can not approve your own pull request"

    def api(self, method, route, fields=None):
        self.calls.append((method, route, dict(fields or {})))
        if route.endswith("/pulls/7"):
            return self.pr
        if "/check-runs" in route:
            return {"check_runs": self.checks}
        if route.endswith("/reviews?per_page=100"):
            return self.reviews
        if route.endswith("/reviews") and method == "POST":
            if self.approve_error:
                raise am.GhError(self.approve_error)
            return {}
        if route.endswith("/protection"):
            raise am.GhError("HTTP 404: Branch not protected")
        if route.endswith("/update-branch"):
            self.pr = {**self.pr, "mergeable_state": "clean", "head": {**self.pr["head"], "sha": "h2"}}
            self.checks = [{"name": "ci", "status": "queued", "conclusion": None}]
            return {}
        return {}

    def gh(self, args):
        self.calls.append(("gh", *args))
        if args[:2] == ["api", "graphql"]:
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}})
        return ""


class FinishTest(unittest.TestCase):
    def run_finish(self, fake, dry=False):
        args = argparse.Namespace(pr=7, wait=False, timeout=60, dry_run=dry, force=False)
        policy = am.derive_policy(True, False)
        with (
            mock.patch.object(am, "gh_api", fake.api),
            mock.patch.object(am, "gh", fake.gh),
            mock.patch.object(am, "repo_slug", return_value="o/r"),
            mock.patch.object(am, "current_policy", return_value=(am.Resolution(True, "test", "true"), policy)),
            mock.patch.object(am, "delete_merged_branch", return_value="deleted") as deleted,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rc = am.cmd_finish(args, sleep=lambda _: None)
        return rc, deleted

    def merges(self, fake):
        return [c for c in fake.calls if c[0] == "PUT" and c[1].endswith("/merge")]

    def test_happy_path_merges_the_checked_head(self):
        fake = FakeGitHub()
        rc, deleted = self.run_finish(fake)
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.merges(fake), [("PUT", "repos/o/r/pulls/7/merge", {"merge_method": "rebase", "sha": "h1"})]
        )
        deleted.assert_called_once()

    def test_no_check_runs_does_not_merge(self):
        fake = FakeGitHub()
        fake.checks = []
        rc, _ = self.run_finish(fake)
        self.assertEqual(rc, 2)
        self.assertEqual(self.merges(fake), [])

    def test_behind_updates_branch_and_waits(self):
        fake = FakeGitHub(mergeable_state="behind")
        rc, _ = self.run_finish(fake)
        self.assertEqual(rc, 2)
        self.assertIn(("PUT", "repos/o/r/pulls/7/update-branch", {"expected_head_sha": "h1"}), fake.calls)
        self.assertEqual(self.merges(fake), [])

    def test_changes_requested_blocks(self):
        fake = FakeGitHub()
        fake.reviews = [{"user": {"login": "maintainer"}, "state": "CHANGES_REQUESTED"}]
        rc, _ = self.run_finish(fake)
        self.assertEqual(rc, 1)
        self.assertEqual(self.merges(fake), [])

    def test_fork_is_not_merged_nor_deleted(self):
        fake = FakeGitHub(head={"sha": "h1", "ref": "fix/1-x", "repo": {"full_name": "someone/r"}})
        rc, deleted = self.run_finish(fake)
        self.assertEqual(rc, 1)
        self.assertEqual(self.merges(fake), [])
        deleted.assert_not_called()

    def test_failed_check_blocks(self):
        fake = FakeGitHub()
        fake.checks = [{"name": "ci", "status": "completed", "conclusion": "failure"}]
        rc, _ = self.run_finish(fake)
        self.assertEqual(rc, 1)
        self.assertEqual(self.merges(fake), [])

    def test_dry_run_changes_nothing(self):
        fake = FakeGitHub(draft=True)
        rc, deleted = self.run_finish(fake, dry=True)
        self.assertEqual(rc, 0)
        self.assertFalse([c for c in fake.calls if c[0] in ("PUT", "POST", "DELETE")])
        self.assertFalse([c for c in fake.calls if c[:3] == ("gh", "pr", "ready")])
        deleted.assert_not_called()

    def test_disabled_auto_pilot_refuses(self):
        args = argparse.Namespace(pr=7, wait=False, timeout=60, dry_run=False, force=False)
        off = (am.Resolution(False, ".env", "false"), am.derive_policy(False, False))
        with (
            mock.patch.object(am, "current_policy", return_value=off),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(am.cmd_finish(args), 1)


class BranchNameTest(unittest.TestCase):
    def test_valid_and_exempt(self):
        for name in ("feat/42-graphify-edge-gate", "docs/readme-badges", "fix/7-x"):
            self.assertEqual(am.validate_branch_name(name).status, "valid", name)
        for name in ("main", "dependabot/pip/foo-1.2"):
            self.assertEqual(am.validate_branch_name(name).status, "exempt", name)

    def test_invalid(self):
        for name in (
            "claude/friendly-pasteur-405hsb",
            "ccr-4c21179f-3zdi34",
            "feature/x",
            "feat/Upper",
            "feat/under_score",
            "feat/" + "a" * 60,
        ):
            self.assertEqual(am.validate_branch_name(name).status, "invalid", name)

    def test_slugify(self):
        self.assertEqual(am.slugify("Add the graphify cross-check gate!"), "add-graphify-cross-check-gate")
        self.assertEqual(am.slugify("Café déjà vu"), "cafe-deja-vu")
        self.assertEqual(am.slugify("日本語だけ"), "")
        self.assertLessEqual(len(am.slugify("word " * 30)), am.MAX_SLUG_LENGTH)

    def test_build_from_conventional_title(self):
        t, text = am.parse_conventional_title("feat(gates): add G-GRAPHIFY")
        self.assertEqual(t, "feat")
        self.assertEqual(am.build_branch_name(t, 12, text), "feat/12-add-g-graphify")
        self.assertEqual(am.build_branch_name("docs", None, "Update README"), "docs/update-readme")
        with self.assertRaises(ValueError):
            am.build_branch_name("feature", None, "x")


if __name__ == "__main__":
    unittest.main()
