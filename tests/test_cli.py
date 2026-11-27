"""CLI smoke tests on a temporary database. Nothing is called for real."""

import pytest
from typer.testing import CliRunner

from salesagent import cli
from salesagent.config import get_settings


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    get_settings.cache_clear()
    result = CliRunner().invoke(cli.app, ["db", "init"])
    assert result.exit_code == 0, result.output
    yield CliRunner()
    get_settings.cache_clear()


def test_demo_round_dry_run_and_report(runner):
    result = runner.invoke(cli.app, ["demo", "--bedrijven", "40"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        cli.app, ["ronde", "maak", "--merk", "Voltera Bedrijfswagens", "--naam", "Droog"]
    )
    assert result.exit_code == 0, result.output
    assert runner.invoke(cli.app, ["ronde", "voorbereiden", "3"]).exit_code == 0

    result = runner.invoke(cli.app, ["ronde", "bellen", "3", "--aantal", "2"])
    assert result.exit_code == 0, result.output
    assert "dry run: 2" in result.output
    assert "AI-assistent" in result.output
    assert "*" in result.output  # number is masked

    result = runner.invoke(cli.app, ["ronde", "bellen", "3", "--modus", "live", "--aantal", "1"])
    assert "DRY_RUN staat aan" in result.output

    result = runner.invoke(cli.app, ["rapport"])
    assert result.exit_code == 0, result.output
    assert "Kosten per afspraak" in result.output


def test_agent_eval_and_prompt(runner):
    result = runner.invoke(cli.app, ["agent", "eval"])
    assert result.exit_code == 0, result.output
    assert "Harde criteria" in result.output
    result = runner.invoke(cli.app, ["agent", "prompt"])
    assert "{{ai_melding}}" in result.output
