import pytest
import shutil
import subprocess

from scripts import autodeploy
from scripts.autodeploy import ci_succeeded


def run(**values):
    return {
        "id": 2,
        "head_sha": "a" * 40,
        "head_branch": "develop",
        "event": "push",
        "path": ".github/workflows/check.yml",
        "status": "completed",
        "conclusion": "success",
        **values,
    }


@pytest.mark.parametrize(
    "values",
    [
        {"status": "in_progress", "conclusion": None},
        {"conclusion": "failure"},
        {"head_sha": "b" * 40},
        {"head_branch": "main"},
        {"event": "pull_request"},
        {"path": ".github/workflows/unrelated.yml"},
    ],
)
def test_autodeploy_requires_success_of_exact_develop_push(values):
    assert not ci_succeeded({"workflow_runs": [run(**values)]}, "a" * 40)


def test_new_pending_attempt_blocks_an_older_success():
    assert not ci_succeeded(
        {"workflow_runs": [run(status="queued", conclusion=None), run(id=1)]}, "a" * 40
    )
    assert ci_succeeded({"workflow_runs": [run()]}, "a" * 40)
    assert not ci_succeeded({"workflow_runs": []}, "a" * 40)


@pytest.mark.skipif(shutil.which("git") is None, reason="Git executable is required")
def test_real_checkout_waits_for_ci_and_rejects_a_rollback(tmp_path, monkeypatch):
    source, remote, checkout = [tmp_path / name for name in ("source", "remote.git", "checkout")]

    def git(*args):
        return subprocess.check_output(["git", *map(str, args)], text=True).strip()

    git("init", "-b", "develop", source)
    git("-C", source, "config", "user.email", "fixture@example.test")
    git("-C", source, "config", "user.name", "Fixture")
    (source / "copy.txt").write_text("one")
    git("-C", source, "add", ".")
    git("-C", source, "commit", "-m", "First")
    first = git("-C", source, "rev-parse", "HEAD")
    (source / "copy.txt").write_text("two")
    git("-C", source, "commit", "-am", "Second")
    second = git("-C", source, "rev-parse", "HEAD")
    git("clone", "--bare", source, remote)
    monkeypatch.setattr(autodeploy, "REMOTE", str(remote))
    monkeypatch.setattr(autodeploy, "check_ci", lambda sha: False)
    releases = tmp_path / "releases"
    (releases / first).mkdir(parents=True)
    (releases / second).mkdir()
    current = tmp_path / "current"
    current.symlink_to(releases / first, target_is_directory=True)
    assert autodeploy.run_once(checkout, current_link=current) == "waiting_for_checks"
    assert current.resolve().name == first
    monkeypatch.setattr(autodeploy, "check_ci", lambda sha: True)
    assert autodeploy.run_once(checkout, current_link=current, plan=True) == "ready:" + second
    # A later operator release cannot be replaced by an older develop head.
    git("--git-dir", remote, "update-ref", "refs/heads/develop", first)
    current.unlink()
    current.symlink_to(releases / second, target_is_directory=True)
    with pytest.raises(RuntimeError, match="not a descendant"):
        autodeploy.run_once(checkout, current_link=current)
    assert current.resolve().name == second
