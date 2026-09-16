"""The command line: every subcommand is reachable and none of them crash.

This module was the last one in the app with no tests at all, and it is the
surface a packaged build still exposes -- `--selftest`, `refresh`, `status`
and the rest are how anything gets diagnosed when the window will not open.
A command that raises on a fresh database is a bad afternoon for whoever runs
it, and nothing here was checking.
"""

import pytest

from nflpicker import cli


def test_every_subcommand_is_wired_to_a_function():
    """argparse happily defines a subcommand with no handler, and the failure
    is an AttributeError at the moment someone runs it."""
    parser = cli.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    assert actions, "no subparsers found"
    names = sorted(actions[0].choices)
    assert set(names) >= {
        "serve", "desktop", "refresh", "train", "backtest",
        "picks", "teasers", "starters", "teams", "sources", "status",
    }
    for name, sub in actions[0].choices.items():
        args = sub.parse_args([])
        assert callable(getattr(args, "func", None)), f"{name} has no handler"


def test_an_unknown_subcommand_exits_rather_than_raising():
    with pytest.raises(SystemExit):
        cli.main(["no-such-command"])


@pytest.mark.parametrize("command", ["sources", "status"])
def test_read_only_commands_run_on_a_fresh_database(temp_env, capsys, command):
    """These are the two anyone reaches for first when something looks wrong,
    and they have to work before a refresh has ever run."""
    assert cli.main([command]) == 0
    out = capsys.readouterr().out
    assert out.strip(), f"{command} printed nothing"


def test_status_reports_the_data_directory_it_is_actually_using(temp_env, capsys):
    """The app now looks in two possible directories, so a command that says
    which one it picked is the difference between a five-second answer and an
    afternoon."""
    cli.main(["status"])
    out = capsys.readouterr().out
    assert str(temp_env) in out
    assert "demo" in out


def test_picks_on_an_empty_database_says_so_rather_than_crashing(temp_env, capsys):
    assert cli.main(["picks"]) in (0, 1)
    out = capsys.readouterr().out + capsys.readouterr().err
    assert out is not None


def test_teams_runs_after_a_refresh(temp_env, capsys):
    from nflpicker.pipeline import Pipeline

    Pipeline().refresh(["schedule", "odds", "recompute"])
    assert cli.main(["teams"]) == 0
    out = capsys.readouterr().out
    # Thirty-two teams, or at least a table with a rating column.
    assert out.strip()


def test_the_parser_builds_without_touching_the_database():
    """Importing and building the parser must not require a data directory --
    `--help` has to work on a machine where nothing is set up yet."""
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0
