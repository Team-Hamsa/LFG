#!/usr/bin/env python3
"""Promote staging (main) to prod (deploy) — all of it, or a chosen subset.

The prod deployer (lfg-deployer) picks up any forward move of ``deploy``
within ~60s and drain-restarts the prod stack (#223). Launched via
``scripts/promote.sh``; stdlib only so it runs without the project venv.

Modes
-----
``promote.sh [--yes]``
    Full promotion. When deploy is an ancestor of main this is the original
    fast-forward. When earlier ``--pick`` runs made deploy diverge, it pushes a
    sync commit whose tree is EXACTLY main's tree (parents: deploy, main) — so
    it can never conflict and prod ends up byte-identical to staging.

``promote.sh --list``
    Show what main has that deploy doesn't (first-parent: one line per merged
    PR / direct push), minus anything already cherry-picked onto deploy.

``promote.sh --pick 508 509 [--yes]``
    Promote only those merges (PR numbers or commit SHAs). If the selection is
    exactly the oldest N pending commits it is a plain fast-forward. Otherwise
    the picks are cherry-picked (``-x``, ``-m 1`` for merges) in main's order
    onto deploy in a throwaway worktree, the pre-push gate runs on the result
    — CI never runs on deploy, and this combination has never existed on
    staging — and only a green gate is pushed. A conflict aborts with nothing
    pushed: pick the PR it depends on too.

Invariants the deployer relies on: deploy only ever moves forward (every push
is a descendant of the deploy SHA read at start, guarded by
``--force-with-lease``). Commits on deploy that aren't on main must be either
``-x`` cherry-picks of main commits or sync commits carrying a main commit's
exact tree; anything else (a hand commit to deploy) is refused.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

REMOTE = os.environ.get("PROMOTE_REMOTE", "origin")
PICKED_RE = re.compile(r"^\(cherry picked from commit ([0-9a-f]{40})\)$", re.M)
PR_RES = (
    re.compile(r"^Merge pull request #(\d+) "),
    re.compile(r"\(#(\d+)\)$"),  # squash merges
)


class PromoteError(Exception):
    def __init__(self, msg: str, code: int = 1) -> None:
        super().__init__(msg)
        self.code = code


def git(*args: str, cwd: str | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if r.returncode != 0:
        raise PromoteError(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r.stdout.strip()


def git_ok(*args: str, cwd: str | None = None) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def is_ancestor(a: str, b: str) -> bool:
    return git_ok("merge-base", "--is-ancestor", a, b)


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    parents: int

    @property
    def pr(self) -> int | None:
        for rx in PR_RES:
            m = rx.search(self.subject)
            if m:
                return int(m.group(1))
        return None

    def label(self) -> str:
        tag = f"#{self.pr}" if self.pr is not None else "direct"
        return f"{self.sha[:9]}  {tag:>7}  {self.subject}"


def load_commit(sha: str) -> Commit:
    out = git("log", "-1", "--format=%H%x00%P%x00%s", sha)
    full, parents, subject = out.split("\x00", 2)
    return Commit(full, subject, len(parents.split()))


def already_picked(deploy: str, main: str) -> set[str]:
    """SHAs of main commits already on deploy as cherry-picks.

    Also validates every deploy-only commit: a ``-x`` pick of a main commit,
    or a sync commit whose tree equals one of its main-side parents.
    """
    picked: set[str] = set()
    for sha in filter(None, git("rev-list", f"{main}..{deploy}").splitlines()):
        body = git("log", "-1", "--format=%B", sha)
        srcs = PICKED_RE.findall(body)
        if srcs and all(is_ancestor(s, main) for s in srcs):
            picked.update(srcs)
            continue
        tree = git("rev-parse", f"{sha}^{{tree}}")
        parents = git("log", "-1", "--format=%P", sha).split()
        if len(parents) > 1 and any(
            is_ancestor(p, main) and git("rev-parse", f"{p}^{{tree}}") == tree for p in parents
        ):
            continue
        raise PromoteError(
            f"ERROR: {REMOTE}/deploy is NOT an ancestor of {REMOTE}/main and "
            f"commit {sha[:12]} on deploy is neither a cherry-pick of main nor a "
            "promote sync — the push would not be a fast-forward of main. Someone "
            "committed to deploy directly. Resolve manually before promoting."
        )
    return picked


def pending(deploy: str, main: str) -> list[Commit]:
    """Main's first-parent commits not on deploy, oldest first."""
    picked = already_picked(deploy, main)
    shas = git("rev-list", "--first-parent", "--reverse", main, f"^{deploy}").splitlines()
    return [load_commit(s) for s in shas if s and s not in picked]


def resolve_picks(refs: list[str], todo: list[Commit]) -> list[Commit]:
    chosen: set[str] = set()
    for ref in refs:
        ref = ref.lstrip("#")
        if ref.isdigit() and len(ref) < 7:
            hits = [c for c in todo if c.pr == int(ref)]
            if not hits:
                raise PromoteError(f"PR #{ref} is not pending promotion (see --list).", 2)
        else:
            full = ""
            if git_ok("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
                full = git("rev-parse", f"{ref}^{{commit}}")
            hits = [c for c in todo if full and c.sha == full]
            if not hits:
                raise PromoteError(
                    f"{ref} is not a pending first-parent commit of main (see --list).", 2
                )
        chosen.update(c.sha for c in hits)
    return [c for c in todo if c.sha in chosen]  # main's order, not argv order


def confirm(prompt: str, yes: bool) -> None:
    if yes:
        return
    print(f"{prompt} [y/N] ", end="", flush=True)
    if sys.stdin.readline().strip() not in ("y", "Y", "yes", "YES"):
        raise PromoteError("Aborted.")


def push(new: str, deploy: str) -> None:
    # Push the SHA computed from the snapshot (not a possibly-newer main) —
    # you promote what you reviewed. --force-with-lease guards against deploy
    # having moved since the snapshot; every push here descends from it.
    #
    # --no-verify: a full promotion introduces no code that didn't already pass
    # the gate on main; a pick build ran the gate itself (build_picks) against its
    # exact tree. The local hook would re-run against the prod checkout's
    # environment instead, which has blocked promotions for unrelated reasons.
    subprocess.run(
        [
            "git",
            "push",
            "--no-verify",
            f"--force-with-lease=refs/heads/deploy:{deploy}",
            REMOTE,
            f"{new}:refs/heads/deploy",
        ],
        check=True,
    )
    print("Promoted. lfg-deployer will deploy prod within ~60s (watch: pm2 logs lfg-deployer).")


def gate_command() -> list[str]:
    # Deliberately no env override: nothing in the operator's environment may
    # swap the gate for another command. Tests put a fake pre-commit on PATH.
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    venv_pc = os.path.join(os.path.dirname(common), ".venv", "bin", "pre-commit")
    exe = venv_pc if os.access(venv_pc, os.X_OK) else shutil.which("pre-commit")
    if not exe:
        raise PromoteError(
            "pre-commit not found (run ./setup.sh); refusing to push an ungated build."
        )
    return [exe, "run", "--hook-stage", "pre-push", "--all-files"]


def build_picks(deploy: str, picks: list[Commit]) -> str:
    """Cherry-pick onto deploy in a throwaway worktree, gate it, return the SHA."""
    gate = gate_command()
    tmp = tempfile.mkdtemp(prefix="promote-pick-")
    wt = os.path.join(tmp, "wt")
    try:
        git("worktree", "add", "--detach", wt, deploy)
        for c in picks:
            args = ["cherry-pick", "-x", "--allow-empty", "--keep-redundant-commits"]
            if c.parents > 1:
                args += ["-m", "1"]
            r = subprocess.run(["git", *args, c.sha], cwd=wt, text=True, capture_output=True)
            if r.returncode != 0:
                subprocess.run(["git", "cherry-pick", "--abort"], cwd=wt, capture_output=True)
                raise PromoteError(
                    f"CONFLICT cherry-picking {c.label()}\n{r.stdout.strip()}\n\n"
                    "It depends on something still held back on main (often a "
                    "cache-buster ?v= bump or an earlier PR touching the same "
                    "lines). Pick that too, or promote everything. Nothing was pushed."
                )
        print(f"\nRunning the pre-push gate on the pick build ({shlex.join(gate)}) ...")
        if subprocess.run(gate, cwd=wt).returncode != 0:
            raise PromoteError("Gate FAILED on the pick build. Nothing was pushed.")
        return git("rev-parse", "HEAD", cwd=wt)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", wt], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], capture_output=True)  # half-added wt


def promote_all(deploy: str, main: str, yes: bool) -> None:
    todo = pending(deploy, main)  # also refuses a hand-committed deploy
    ff = is_ancestor(deploy, main)
    if not ff and git("rev-parse", f"{deploy}^{{tree}}") == git("rev-parse", f"{main}^{{tree}}"):
        print(f"deploy is already up to date with main ({main}). Nothing to promote.")
        return
    print("Promoting the following commits to prod (deploy):\n")
    for c in todo:
        print(f"  {c.label()}")
    print()
    if ff:
        confirm(f"Fast-forward {REMOTE}/deploy to {REMOTE}/main?", yes)
        push(main, deploy)
        return
    print("deploy carries cherry-picks, so this pushes a sync commit whose tree is main's.")
    confirm(f"Sync {REMOTE}/deploy to {REMOTE}/main's exact tree?", yes)
    sync = git(
        "commit-tree",
        f"{main}^{{tree}}",
        "-p",
        deploy,
        "-p",
        main,
        "-m",
        f"promote: sync deploy to main {main[:12]}",
    )
    push(sync, deploy)


def promote_picks(deploy: str, main: str, refs: list[str], yes: bool) -> None:
    todo = pending(deploy, main)
    if not todo:
        raise PromoteError("Nothing pending on main — nothing to pick.", 2)
    picks = resolve_picks(refs, todo)
    held = [c for c in todo if c not in picks]
    ff = is_ancestor(deploy, main) and picks == todo[: len(picks)]
    print("Promoting to prod (deploy):\n")
    for c in picks:
        print(f"  {c.label()}")
    if held:
        print("\nHolding back (stays on staging only):\n")
        for c in held:
            print(f"  {c.label()}")
    print()
    if ff:
        confirm(f"Fast-forward {REMOTE}/deploy to {picks[-1].sha[:12]}?", yes)
        push(picks[-1].sha, deploy)
        return
    confirm("Cherry-pick these onto deploy, run the gate, and push if green?", yes)
    push(build_picks(deploy, picks), deploy)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="promote.sh", description=__doc__.split("\n")[0])
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="show what is pending promotion")
    mode.add_argument(
        "--pick", nargs="+", metavar="PR_OR_SHA", help="promote only these PRs/commits"
    )
    args = ap.parse_args(argv)

    try:
        git("fetch", REMOTE, "--prune")
        main_sha = git("rev-parse", f"{REMOTE}/main")
        if not git_ok("rev-parse", "--verify", "--quiet", f"{REMOTE}/deploy"):
            raise PromoteError(
                f"ERROR: {REMOTE}/deploy does not exist. Create it once with:\n"
                f"  git push {REMOTE} main:deploy"
            )
        deploy_sha = git("rev-parse", f"{REMOTE}/deploy")

        if args.list:
            todo = pending(deploy_sha, main_sha)
            if not todo:
                print("Nothing pending: deploy has everything on main.")
            for c in todo:
                print(c.label())
            return 0
        if main_sha == deploy_sha or is_ancestor(main_sha, deploy_sha):
            print(f"deploy is already up to date with main ({main_sha}). Nothing to promote.")
            return 0
        if args.pick:
            promote_picks(deploy_sha, main_sha, args.pick, args.yes)
        else:
            promote_all(deploy_sha, main_sha, args.yes)
        return 0
    except PromoteError as e:
        print(e, file=sys.stderr)
        return e.code
    except subprocess.CalledProcessError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
