from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path


def test_gitpython_is_not_a_core_dependency() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    assert not any(dependency.lower().startswith("gitpython") for dependency in dependencies)
    assert any(
        dependency.lower().startswith("gitpython")
        for dependency in pyproject["project"]["optional-dependencies"]["legacy-git"]
    )


def test_core_server_imports_and_builds_when_gitpython_is_blocked(tmp_path: Path) -> None:
    script = r'''
import importlib.abc
import os
import sys

class BlockGit(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "git" or fullname.startswith("git."):
            raise ImportError("GitPython intentionally unavailable")
        return None

sys.meta_path.insert(0, BlockGit())
for name in tuple(sys.modules):
    if name == "git" or name.startswith("git."):
        del sys.modules[name]
os.environ["PATH"] = ""
from mcp_agent_mail.app import build_mcp_server
server = build_mcp_server()
assert server is not None
assert not any(name == "git" or name.startswith("git.") for name in sys.modules)
'''
    environment = dict(os.environ)
    environment["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'core.sqlite3'}"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
