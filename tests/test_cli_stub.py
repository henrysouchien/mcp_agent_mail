"""Tests for the installed canonical CLI launcher."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def cli_launcher_script(tmp_path: Path) -> Path:
    """Create a launcher backed by a fake Python executable."""
    python_path = tmp_path / "python"
    python_path.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n")
    python_path.chmod(0o755)
    launcher_path = tmp_path / "mcp-agent-mail"
    launcher_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'exec "{python_path}" -m mcp_agent_mail.cli "$@"\n'
    )
    launcher_path.chmod(0o755)
    return launcher_path


def test_cli_launcher_dispatches_to_real_module(cli_launcher_script: Path):
    result = subprocess.run(
        [str(cli_launcher_script), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["-m", "mcp_agent_mail.cli", "--help"]


def test_cli_launcher_preserves_arguments(cli_launcher_script: Path):
    result = subprocess.run(
        [str(cli_launcher_script), "mail", "status", ".", "--json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines()[-4:] == ["mail", "status", ".", "--json"]


class TestInstallScriptCliLauncher:
    """Tests for the install_cli_launcher function in install.sh."""

    def test_install_function_exists(self):
        """Verify the install_cli_launcher function exists in install.sh."""
        install_script = Path(__file__).parent.parent / "scripts" / "install.sh"
        content = install_script.read_text()

        assert "install_cli_launcher()" in content
        assert 'exec "\\${python_path}" -m mcp_agent_mail.cli "\\$@"' in content

    def test_install_creates_variants(self):
        """Verify install script creates variant symlinks."""
        install_script = Path(__file__).parent.parent / "scripts" / "install.sh"
        content = install_script.read_text()

        # Should create symlinks for common variants
        expected_variants = ["mcp_agent_mail", "mcpagentmail", "agentmail", "agent-mail"]
        for variant in expected_variants:
            assert variant in content, f"Should create symlink for '{variant}'"

    def test_launcher_does_not_claim_the_cli_is_missing(self):
        install_script = Path(__file__).parent.parent / "scripts" / "install.sh"
        content = install_script.read_text()
        assert "is NOT a CLI tool" not in content
