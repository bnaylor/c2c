import subprocess
import pytest
from c2c.ferry import projectid as p


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:sackheads/iris.git", "github.com/sackheads/iris"),
    ("https://github.com/sackheads/iris.git", "github.com/sackheads/iris"),
    ("https://github.com/sackheads/iris", "github.com/sackheads/iris"),
    ("ssh://git@github.com/sackheads/iris.git", "github.com/sackheads/iris"),
    ("git@GitHub.com:Sackheads/iris.git", "github.com/Sackheads/iris"),
    ("https://gitlab.example.com:8443/g/sub/repo.git", "gitlab.example.com/g/sub/repo"),
])
def test_normalize_remote(url, expected):
    assert p.normalize_remote(url) == expected


def test_normalize_lowercases_host_not_path():
    assert p.normalize_remote("git@GITHUB.com:A/B.git") == "github.com/A/B"


def test_project_for_cwd_real_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "remote", "add", "origin",
                    "git@github.com:sackheads/iris.git"], cwd=tmp_path, check=True)
    assert p.project_for_cwd(str(tmp_path)) == "github.com/sackheads/iris"


def test_project_for_cwd_no_repo(tmp_path):
    assert p.project_for_cwd(str(tmp_path)) is None


def test_project_for_cwd_caches_per_cwd(tmp_path, monkeypatch):
    # project_for_cwd is called once per session per delivery/announce from
    # the async orchestrator; it must not re-spawn git on every call for a
    # cwd whose result is already known.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "remote", "add", "origin",
                    "git@github.com:sackheads/iris.git"], cwd=tmp_path, check=True)
    p._cache.clear()
    real_run = subprocess.run
    calls = []

    def counting_run(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(p.subprocess, "run", counting_run)
    try:
        first = p.project_for_cwd(str(tmp_path))
        second = p.project_for_cwd(str(tmp_path))
        assert first == second == "github.com/sackheads/iris"
        assert len(calls) == 1
    finally:
        p._cache.clear()


def test_project_for_cwd_does_not_cache_none(tmp_path, monkeypatch):
    # A directory that isn't a repo yet must NOT be cached, so a repo cloned
    # there after the ferry started gets picked up without a restart.
    p._cache.clear()
    calls = []
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(p.subprocess, "run", counting_run)
    try:
        assert p.project_for_cwd(str(tmp_path)) is None   # not a repo -> git ran
        # now it becomes a repo:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "remote", "add", "origin",
                        "git@github.com:sackheads/iris.git"], cwd=tmp_path, check=True)
        # ...and the next call picks it up (None was never cached):
        assert p.project_for_cwd(str(tmp_path)) == "github.com/sackheads/iris"
        # project_for_cwd ran git on BOTH calls (None wasn't cached). Filter to
        # its `remote get-url` invocation so the test's own `git remote add`
        # (also routed through the patched subprocess.run) isn't miscounted.
        geturl_calls = [c for c in calls if "get-url" in c[0]]
        assert len(geturl_calls) == 2
    finally:
        p._cache.clear()
