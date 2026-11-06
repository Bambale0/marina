"""Install only a successful Checks push on develop into the Marina preview."""

import argparse
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

REPOSITORY = "Bambale0/marina"
REMOTE = "https://github.com/" + REPOSITORY + ".git"


def ci_succeeded(payload, sha):
    runs = payload.get("workflow_runs", [])
    if not runs:
        return False
    latest = max(runs, key=lambda run: run["id"])
    return all(
        (
            latest.get("head_sha") == sha,
            latest.get("head_branch") == "develop",
            latest.get("event") == "push",
            latest.get("path", "").split("@", 1)[0] == ".github/workflows/check.yml",
            latest.get("status") == "completed",
            latest.get("conclusion") == "success",
        )
    )


def git(checkout, *args):
    return subprocess.check_output(
        ["git", "-C", str(checkout), *args], text=True, timeout=90
    ).strip()


def check_ci(sha):
    query = urllib.parse.urlencode(
        {"branch": "develop", "event": "push", "head_sha": sha, "per_page": 10}
    )
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/check.yml/runs?{query}",
        headers={
            "User-Agent": "Marina-preview-autodeploy",
            "Accept": "application/vnd.github+json",
        },
    )
    # The repository is public; no GitHub token or inbound deployment endpoint is required.
    with urllib.request.urlopen(request, timeout=20) as response:
        return ci_succeeded(json.load(response), sha)


def run_once(checkout, *, plan=False, current_link="/opt/marina/current"):
    checkout = Path(checkout).resolve()
    if not checkout.exists():
        subprocess.run(
            ["git", "clone", "--single-branch", "--branch", "develop", REMOTE, str(checkout)],
            check=True,
            timeout=120,
        )
    if git(checkout, "remote", "get-url", "origin") != REMOTE:
        raise RuntimeError("Unexpected deployment repository")
    if git(checkout, "status", "--porcelain"):
        raise RuntimeError("Deployment checkout contains local changes")
    git(checkout, "fetch", "--no-tags", "origin", "+refs/heads/develop:refs/remotes/origin/develop")
    sha = git(checkout, "rev-parse", "refs/remotes/origin/develop")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("Invalid deployment revision")
    current_link = Path(current_link)
    current = current_link.resolve(strict=True)
    if current.name == sha:
        return "unchanged"
    if not re.fullmatch(r"[0-9a-f]{40}", current.name):
        raise RuntimeError("Current release is not an immutable Git revision")
    # A delayed CI run must never roll back a newer release installed by another operator.
    if subprocess.run(
        ["git", "-C", str(checkout), "merge-base", "--is-ancestor", current.name, sha], timeout=30
    ).returncode:
        raise RuntimeError("Candidate is not a descendant of the current release")
    if not check_ci(sha):
        return "waiting_for_checks"
    remote_head = git(checkout, "ls-remote", "origin", "refs/heads/develop").split()[0]
    if remote_head != sha:
        return "develop_moved"
    if plan:
        return "ready:" + sha
    git(checkout, "checkout", "--detach", sha)
    environment = {**os.environ, "MARINA_EXPECTED_CURRENT": str(current)}
    subprocess.run(
        ["bash", str(checkout / "scripts/deploy.sh"), sha],
        env=environment,
        check=True,
        timeout=1200,
    )
    if current_link.resolve() != current.parent / sha:
        raise RuntimeError("Deployment did not activate the expected revision")
    return "deployed:" + sha


def main():
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", default="/opt/marina/autodeploy-source")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    with open("/run/lock/marina-autodeploy.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        print(run_once(args.checkout, plan=args.plan), flush=True)


if __name__ == "__main__":
    main()
