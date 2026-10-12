import pytest


@pytest.fixture(autouse=True)
def isolate_jev_home(tmp_path, monkeypatch):
    """Autouse fixture setting JEV_HOME to a per-test temporary directory.

    Ensures tests write ledgers and escalation artifacts into an isolated temporary
    directory rather than the real ~/.jev.
    """
    jev_home = tmp_path / "jev_home"
    jev_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JEV_HOME", str(jev_home))
    return jev_home
