"""Map a working directory to a stable project id via its git origin URL."""
from __future__ import annotations

import functools
import re
import subprocess

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def normalize_remote(url: str) -> str:
    u = url.strip()
    if u.endswith(".git"):
        u = u[:-4]
    if _SCHEME.match(u):
        u = _SCHEME.sub("", u)          # drop scheme://
        if "@" in u.split("/", 1)[0]:
            u = u.split("@", 1)[1]       # drop user@ from authority
        host, _, path = u.partition("/")
        host = host.split(":", 1)[0]     # drop :port
    elif "@" in u and ":" in u.split("/", 1)[0]:
        # scp form: user@host:owner/repo
        u = u.split("@", 1)[1]
        host, _, path = u.partition(":")
    else:
        host, _, path = u.partition("/")
        host = host.split(":", 1)[0]
    return f"{host.lower()}/{path.strip('/')}"


@functools.lru_cache(maxsize=None)
def project_for_cwd(cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return normalize_remote(out.stdout.strip())
