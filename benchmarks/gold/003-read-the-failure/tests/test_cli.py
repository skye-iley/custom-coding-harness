from cli import main


def test_cli_reports_counts():
    assert main(["a", "a", "b"]).splitlines()[0] == "a: 2"


def test_cli_on_empty_input():
    assert main([]) == ""
