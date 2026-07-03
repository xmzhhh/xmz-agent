"""Smoke tests for the command-line entry point."""

import pytest

from finagent.cli import main


def test_main_prints_ready_message(capsys: pytest.CaptureFixture[str]) -> None:
    main()

    captured = capsys.readouterr()
    assert captured.out == "FinAgent CLI is ready.\n"
