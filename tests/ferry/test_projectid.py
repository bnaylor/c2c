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
    p.project_for_cwd.cache_clear()
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
        p.project_for_cwd.cache_clear()
