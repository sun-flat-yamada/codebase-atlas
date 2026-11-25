"""scripts/automerge.py の純粋関数の単体テスト（GitHub には接続しない）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_fork_detection(self):
        self.assertFalse(pull(head_repo="o/r", base_repo="o/r").from_fork)
        self.assertTrue(pull(head_repo="someone/r", base_repo="o/r").from_fork)
        self.assertTrue(pull(head_repo="(deleted fork)", base_repo="o/r").from_fork)

    def test_self_approval(self):
        self.assertTrue(am.is_self_approval_rejection("HTTP 422: Can not approve your own pull request"))
        self.assertFalse(am.is_self_approval_rejection("HTTP 403: forbidden"))


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
