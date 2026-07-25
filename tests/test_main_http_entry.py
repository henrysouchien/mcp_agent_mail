from __future__ import annotations

from typer.testing import CliRunner


def test_cli_help_no_args():
    # Invoking CLI help should succeed
    runner = CliRunner()
    from mcp_agent_mail.cli import app as cli_app
    res = runner.invoke(cli_app, ["--help"])
    assert res.exit_code == 0


def test_cli_no_args_shows_help_without_starting_server(monkeypatch):
    runner = CliRunner()
    from mcp_agent_mail import cli

    monkeypatch.setattr(
        cli,
        "serve_http",
        lambda **_: (_ for _ in ()).throw(AssertionError("must not start server")),
    )
    res = runner.invoke(cli.app, [])
    assert res.exit_code == 2
    assert "Usage:" in res.stdout

