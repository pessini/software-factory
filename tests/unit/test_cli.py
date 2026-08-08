from typer.testing import CliRunner

from software_factory.cli import app


def test_graph_command_prints_topology() -> None:
    result = CliRunner().invoke(app, ["graph", "--format", "mermaid"])
    assert result.exit_code == 0
    assert "bootstrap --> plan --> build --> test" in result.stdout


def test_help_exposes_operational_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("acp", "doctor", "run", "inspect", "resume", "cancel", "repo-check"):
        assert command in result.stdout
