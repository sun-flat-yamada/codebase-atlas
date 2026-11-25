#!/usr/bin/env python3
"""Auto-Pilot merge helper for codebase-atlas pull requests.

Ported from github-copilot-dashboard (`scripts/change-dev-autopilot.ts` and
`scripts/change-dev-branch.ts`) to the Python standard library, so this repository
keeps a zero-dependency toolchain (bash + Python only).

`CHG_DEV_AUTO_PILOT` decides what happens after a pull request is opened:

    true / 1   -> open the PR ready for review, wait for CI, approve, Rebase & Merge,
                  delete the head branch (`finish`)
    otherwise  -> manual (open the PR as a draft and let a human merge it)

Resolution order: process environment -> `.env` -> `.env.example` (repository default).

Only the GitHub REST API is used (`gh api ...`). Claude Code cloud sessions reach GitHub
through a proxy that rejects GraphQL, so `gh pr merge` / `gh pr checks` / `gh pr ready`
do not work there; marking a draft ready uses the proxy's dedicated
`/pulls/{n}/ccr/ready_for_review` route in the cloud and `gh pr ready` locally.

Usage:
    python3 scripts/automerge.py mode
    python3 scripts/automerge.py finish <pr> [--wait] [--dry-run] [--force]
        exit 0 = merged (or mergeable in --dry-run), 2 = CI still running, 1 = stopped
    python3 scripts/automerge.py branch check [<branch>]
    python3 scripts/automerge.py branch name <type> <issue|-> <title...>

Guardrails (never relaxed): never `--admin`, never bypass branch protection, never merge
with a failed or running check, a conflict, or a head SHA that changed after the check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KEY = "CHG_DEV_AUTO_PILOT"

# ---------------------------------------------------------------------------
# Mode resolution (pure)
# ---------------------------------------------------------------------------


def parse_env_file(text: str) -> dict[str, str]:
    """Read `.env` syntax: KEY=value, `export KEY=value`, # comments, quotes."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        quoted = re.match(r"^(['\"])(.*)\1$", value)
        if quoted:
            value = quoted.group(2)
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()
        result[m.group(1)] = value
    return result


def is_auto_pilot_value(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in ("true", "1")


@dataclass(frozen=True)
class Resolution:
    enabled: bool
    source: str  # process-env / .env / .env.example / unset
    raw: str | None


def resolve_auto_pilot(
    env: Mapping[str, str],
    dot_env: Mapping[str, str] | None = None,
    dot_env_example: Mapping[str, str] | None = None,
) -> Resolution:
    for source, layer in (("process-env", env), (".env", dot_env), (".env.example", dot_env_example)):
        raw = (layer or {}).get(KEY)
        if raw is not None and raw != "":
            return Resolution(is_auto_pilot_value(raw), source, raw)
    return Resolution(False, "unset", None)


@dataclass(frozen=True)
class Policy:
    auto_pilot: bool
    cloud: bool
    plan_gate: str  # await-user-approval / proceed-and-report
    pr_draft: bool
    after_pr: str  # auto-merge / manual
    github_transport: str  # rest-via-proxy / gh-cli


def derive_policy(auto_pilot: bool, cloud: bool) -> Policy:
    return Policy(
        auto_pilot=auto_pilot,
        cloud=cloud,
        plan_gate="proceed-and-report" if auto_pilot else "await-user-approval",
        pr_draft=not auto_pilot,
        after_pr="auto-merge" if auto_pilot else "manual",
        github_transport="rest-via-proxy" if cloud else "gh-cli",
    )


def current_policy(env: Mapping[str, str] | None = None, root: Path = REPO_ROOT) -> tuple[Resolution, Policy]:
    env = os.environ if env is None else env

    def read(name: str) -> dict[str, str] | None:
        p = root / name
        return parse_env_file(p.read_text(encoding="utf-8")) if p.is_file() else None

    resolution = resolve_auto_pilot(env, read(".env"), read(".env.example"))
    cloud = env.get("CLAUDE_CODE_REMOTE") == "true"
    return resolution, derive_policy(resolution.enabled, cloud)


# ---------------------------------------------------------------------------
# Merge readiness (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PullSnapshot:
    state: str  # open / closed
    merged: bool
    draft: bool
    mergeable_state: str  # clean / unstable / blocked / behind / dirty / unknown / draft / has_hooks
    head_sha: str
    base_ref: str = "main"
    head_ref: str = ""
    # "owner/name" of the head and base repositories (they differ for a pull request from a fork)
    head_repo: str = ""
    base_repo: str = ""

    @property
    def from_fork(self) -> bool:
        return bool(self.head_repo) and self.head_repo != self.base_repo


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str  # queued / in_progress / completed / waiting / requested / pending
    conclusion: str | None


@dataclass(frozen=True)
class Readiness:
    state: str  # ready / pending / blocked
    reasons: list[str] = field(default_factory=list)


FAILED_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}


def evaluate_merge_readiness(pr: PullSnapshot, checks: list[CheckRun]) -> Readiness:
    """A failed check or a conflict blocks; a running check or unknown mergeability waits."""
    if pr.merged:
        return Readiness("blocked", ["already merged"])
    if pr.state != "open":
        return Readiness("blocked", ["pull request is closed"])
    failed = [c for c in checks if c.status == "completed" and (c.conclusion or "") in FAILED_CONCLUSIONS]
    if failed:
        return Readiness("blocked", [f"check failed: {c.name} ({c.conclusion})" for c in failed])
    if pr.mergeable_state == "dirty":
        return Readiness("blocked", ["merge conflict with the base branch"])
    reasons = [f"check running: {c.name}" for c in checks if c.status != "completed"]
    if pr.mergeable_state == "unknown":
        reasons.append("mergeability is being computed")
    return Readiness("pending", reasons) if reasons else Readiness("ready", [])


def is_self_approval_rejection(message: str) -> bool:
    """GitHub answers 422 when the PR author tries to approve their own pull request."""
    return re.search(r"approve your own pull request", message, re.IGNORECASE) is not None


# ---------------------------------------------------------------------------
# Branch names (pure)
# ---------------------------------------------------------------------------

BRANCH_TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
MAX_SLUG_LENGTH = 40
MAX_BRANCH_LENGTH = 60
EXEMPT = (re.compile(r"^main$"), re.compile(r"^dependabot/"), re.compile(r"^gh-pages$"))
CLOUD_DEFAULT = (
    re.compile(r"^claude/[a-z]+-[a-z]+-[a-z0-9]{4,}$", re.IGNORECASE),
    re.compile(r"^ccr-[0-9a-f]{6,}-[a-z0-9]{4,}$", re.IGNORECASE),
)
BRANCH_PATTERN = re.compile(rf"^({'|'.join(BRANCH_TYPES)})/(?:([1-9][0-9]*)-)?([a-z0-9]+(?:-[a-z0-9]+)*)$")
STOP_WORDS = {"a", "an", "the"}


def slugify(title: str, max_len: int = MAX_SLUG_LENGTH) -> str:
    """Title -> kebab-case ASCII slug cut at a word boundary. '' when nothing ASCII is left."""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", text).split() if w not in STOP_WORDS]
    slug = ""
    for w in words:
        nxt = f"{slug}-{w}" if slug else w
        if len(nxt) > max_len:
            break
        slug = nxt
    return slug or (words[0][:max_len] if words else "")


def parse_conventional_title(title: str) -> tuple[str | None, str]:
    m = re.match(r"^\s*([a-z]+)(?:\([^)]*\))?!?:\s*(.*)$", title)
    if m and m.group(1) in BRANCH_TYPES:
        return m.group(1), m.group(2)
    return None, title


def build_branch_name(branch_type: str, issue: int | None, title: str) -> str:
    if branch_type not in BRANCH_TYPES:
        raise ValueError(f"unknown type {branch_type!r} (use one of: {', '.join(BRANCH_TYPES)})")
    slug = slugify(title)
    if not slug:
        raise ValueError("the title has no ASCII words; pass an English title")
    name = f"{branch_type}/{issue}-{slug}" if issue else f"{branch_type}/{slug}"
    return name


@dataclass(frozen=True)
class BranchCheck:
    status: str  # valid / exempt / invalid
    reasons: list[str] = field(default_factory=list)


def validate_branch_name(branch: str) -> BranchCheck:
    if any(p.search(branch) for p in EXEMPT):
        return BranchCheck("exempt")
    reasons: list[str] = []
    if any(p.search(branch) for p in CLOUD_DEFAULT):
        reasons.append("the Claude Code cloud default name says nothing about the change; rename it")
    if not BRANCH_PATTERN.match(branch):
        types = "|".join(BRANCH_TYPES)
        reasons.append(f"must match <type>/<issue>-<slug> or <type>/<slug> (type: {types}; slug: kebab-case ASCII)")
    else:
        slug = BRANCH_PATTERN.match(branch).group(3)  # type: ignore[union-attr]
        if len(slug) > MAX_SLUG_LENGTH:
            reasons.append(f"slug is longer than {MAX_SLUG_LENGTH} characters")
    if len(branch) > MAX_BRANCH_LENGTH:
        reasons.append(f"branch name is longer than {MAX_BRANCH_LENGTH} characters")
    return BranchCheck("invalid", reasons) if reasons else BranchCheck("valid")


# ---------------------------------------------------------------------------
# GitHub REST via `gh api`
# ---------------------------------------------------------------------------


class GhError(Exception):
    pass


def gh(args: list[str]) -> str:
    try:
        r = subprocess.run(["gh", *args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError as e:
        raise GhError("gh (GitHub CLI) is not installed") from e
    if r.returncode != 0:
        raise GhError((r.stdout or "") + (r.stderr or ""))
    return r.stdout


def gh_api(method: str, route: str, fields: Mapping[str, str] | None = None) -> dict:
    args = ["api", "--method", method, route]
    for k, v in (fields or {}).items():
        args += ["-f", f"{k}={v}"]
    out = gh(args).strip()
    return json.loads(out) if out else {}


def repo_slug() -> str:
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    m = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        raise GhError(f"origin is not a github.com repository: {url}")
    return f"{m.group(1)}/{m.group(2)}"


def read_pull(repo: str, pr: int) -> PullSnapshot:
    p = gh_api("GET", f"repos/{repo}/pulls/{pr}")
    return PullSnapshot(
        state=p["state"],
        merged=bool(p.get("merged")),
        draft=bool(p.get("draft")),
        mergeable_state=p.get("mergeable_state") or "unknown",
        head_sha=p["head"]["sha"],
        base_ref=p["base"]["ref"],
        head_ref=p["head"]["ref"],
        # a fork that was deleted has `head.repo: null`; treat it as a fork (never the base repository)
        head_repo=((p["head"].get("repo") or {}).get("full_name")) or "(deleted fork)",
        base_repo=(p["base"].get("repo") or {}).get("full_name", repo),
    )


def read_check_runs(repo: str, sha: str) -> list[CheckRun]:
    r = gh_api("GET", f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
    return [CheckRun(c["name"], c["status"], c.get("conclusion")) for c in r.get("check_runs", [])]


def required_approvals(repo: str, branch: str) -> int:
    try:
        p = gh_api("GET", f"repos/{repo}/branches/{branch}/protection")
    except GhError:
        return 0
    return int((p.get("required_pull_request_reviews") or {}).get("required_approving_review_count") or 0)


def mark_ready(repo: str, pr: int, cloud: bool) -> None:
    if cloud:
        gh_api("POST", f"repos/{repo}/pulls/{pr}/ccr/ready_for_review")
    else:
        gh(["pr", "ready", str(pr), "--repo", repo])


def approve(repo: str, pr: int, sha: str) -> str:
    try:
        gh_api(
            "POST",
            f"repos/{repo}/pulls/{pr}/reviews",
            {"event": "APPROVE", "commit_id": sha, "body": "Auto-Pilot: quality gate and CI passed on this head."},
        )
        return "approved"
    except GhError as e:
        if is_self_approval_rejection(str(e)):
            return "self"
        raise


def delete_merged_branch(repo: str, head_ref: str, base_ref: str) -> str:
    if not head_ref or head_ref == base_ref or head_ref == "main":
        return "skipped"
    r = subprocess.run(["git", "push", "origin", "--delete", head_ref], cwd=REPO_ROOT, capture_output=True)
    if r.returncode == 0:
        return "deleted"
    try:
        gh_api("DELETE", f"repos/{repo}/git/refs/heads/{head_ref}")
        return "deleted"
    except GhError:
        return "failed"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_mode(_: argparse.Namespace) -> int:
    resolution, policy = current_policy()
    print(json.dumps({KEY: resolution.__dict__, "policy": policy.__dict__}, indent=2))
    return 0


def cmd_finish(args: argparse.Namespace, sleep: Callable[[float], None] = time.sleep) -> int:
    pr = args.pr
    resolution, policy = current_policy()
    if not policy.auto_pilot and not args.force:
        print(
            f"❌ {KEY} is not enabled (source: {resolution.source}). Merge manually, or pass --force.", file=sys.stderr
        )
        return 1
    repo = repo_slug()
    pull = read_pull(repo, pr)

    # Auto-Pilot handles pull requests from branches of this repository only. A fork's code was not written
    # by the agent, and deleting `head_ref` on origin would remove an unrelated branch with the same name.
    if pull.from_fork:
        print(f"🛑 PR #{pr} comes from a fork ({pull.head_repo}); review and merge it manually.", file=sys.stderr)
        return 1

    branch = validate_branch_name(pull.head_ref)
    if branch.status == "invalid":
        print(f"🛑 branch name check failed: {'; '.join(branch.reasons)}", file=sys.stderr)
        return 1
    print(f"✅ branch name: {pull.head_ref}")

    if pull.draft:
        print(f"📝 PR #{pr} is a draft -> marking ready for review")
        if not args.dry_run:
            mark_ready(repo, pr, policy.cloud)

    deadline = time.monotonic() + args.timeout
    while True:
        pull = read_pull(repo, pr)
        readiness = evaluate_merge_readiness(pull, read_check_runs(repo, pull.head_sha))
        if readiness.state != "pending" or not args.wait or time.monotonic() > deadline:
            break
        print(f"⏳ waiting: {'; '.join(readiness.reasons)}", flush=True)
        sleep(30)

    if readiness.state == "pending":
        print(f"⏳ PR #{pr} is not ready yet: {'; '.join(readiness.reasons)}")
        return 2
    if readiness.state == "blocked":
        print(f"🛑 PR #{pr} cannot be merged: {'; '.join(readiness.reasons)}", file=sys.stderr)
        return 1

    required = required_approvals(repo, pull.base_ref)
    if args.dry_run:
        print(f"(dry-run) would approve; {pull.base_ref} requires {required} approval(s)")
        print(f"(dry-run) would rebase-merge PR #{pr} at {pull.head_sha}")
        return 0

    if approve(repo, pr, pull.head_sha) == "self":
        print("ℹ️  GitHub rejected the approval: the PR author cannot approve their own pull request.")
        if required > 0:
            print(f"🛑 {pull.base_ref} requires {required} approval(s); another account must approve.", file=sys.stderr)
            return 1
        print(f"✅ {pull.base_ref} requires 0 approvals -> continuing without an approval.")
    else:
        print("✅ approved")

    # Merge exactly the head that passed CI: a commit pushed after the check is never merged unchecked.
    gh_api("PUT", f"repos/{repo}/pulls/{pr}/merge", {"merge_method": "rebase", "sha": pull.head_sha})
    print(f"🎉 PR #{pr} rebase-merged into {pull.base_ref}")

    deleted = delete_merged_branch(repo, pull.head_ref, pull.base_ref)
    if deleted == "failed":
        print(f"⚠️  could not delete remote branch {pull.head_ref}: delete it manually.", file=sys.stderr)
    elif deleted == "deleted":
        print(f"🧹 deleted remote branch {pull.head_ref}")
    return 0


def current_branch() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def cmd_branch(args: argparse.Namespace) -> int:
    if args.action == "check":
        name = args.values[0] if args.values else current_branch()
        result = validate_branch_name(name)
        if result.status == "invalid":
            print(f"❌ {name}: {'; '.join(result.reasons)}", file=sys.stderr)
            print("   rename: git branch -m <type>/<issue>-<slug>   (e.g. feat/42-graphify-edge-gate)", file=sys.stderr)
            return 1
        print(f"✅ {name} ({result.status})")
        return 0
    # name <type> <issue|-> <title...>
    if len(args.values) < 3:
        print("Usage: automerge.py branch name <type> <issue|-> <title...>", file=sys.stderr)
        return 1
    branch_type, issue_raw, *title_words = args.values
    title = " ".join(title_words)
    parsed_type, text = parse_conventional_title(title)
    if parsed_type and branch_type == "-":
        branch_type = parsed_type
    issue = None if issue_raw in ("-", "") else int(issue_raw)
    try:
        print(build_branch_name(branch_type, issue, text))
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="automerge.py", description="Auto-Pilot merge helper (CHG_DEV_AUTO_PILOT)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("mode", help="print the resolved mode and policy").set_defaults(func=cmd_mode)

    f = sub.add_parser("finish", help="wait for CI, approve, Rebase & Merge, delete the branch")
    f.add_argument("pr", type=int)
    f.add_argument("--wait", action="store_true", help="poll CI every 30 s (local sessions only)")
    f.add_argument("--timeout", type=int, default=30 * 60, help="seconds to wait with --wait (default 1800)")
    f.add_argument("--dry-run", action="store_true")
    f.add_argument("--force", action="store_true", help="run even when Auto-Pilot is disabled")
    f.set_defaults(func=cmd_finish)

    b = sub.add_parser("branch", help="validate or build a <type>/<issue>-<slug> branch name")
    b.add_argument("action", choices=["check", "name"])
    b.add_argument("values", nargs="*")
    b.set_defaults(func=cmd_branch)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GhError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
