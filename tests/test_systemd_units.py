"""
The shipped systemd units, checked as configuration rather than as text.

These files are not imported by anything, so nothing else in the suite would
notice a bad edit — the failure surfaces on a server, after a deploy, as a
service that will not start. Each assertion here stands for an outage that
actually happened.
"""

import configparser
import pathlib

import pytest

UNITS = ("als-backend", "als-worker")


def _path(name: str) -> pathlib.Path:
    path = pathlib.Path(__file__).resolve().parents[1] / "deploy" / f"{name}.service"
    assert path.exists(), f"{path} is missing"
    return path


def _unit(name: str) -> configparser.ConfigParser:
    # systemd allows a key to repeat; configparser does not. strict=False keeps
    # the last occurrence, which is correct for the single-valued settings read
    # through this helper — but not for Environment, which systemd treats as
    # additive. Use _environment() for that.
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read(_path(name), encoding="utf-8")
    return parser


def _environment(name: str) -> str:
    """Every Environment= line joined, the way systemd accumulates them."""
    return " ".join(
        line.split("=", 1)[1].strip()
        for line in _path(name).read_text(encoding="utf-8").splitlines()
        if line.startswith("Environment=")
    )


@pytest.mark.parametrize("name", UNITS)
def test_home_is_set_away_from_protected_home(name: str) -> None:
    """
    The obscure one, and the reason this file exists.

    With ``ProtectHome=true`` the service cannot stat anything under /home.
    asyncpg, whenever TLS is in play, builds a default client-key path of
    ``$HOME/.postgresql/postgresql.key`` and calls ``.exists()`` on it — which
    raises ``PermissionError`` instead of returning False. The connection then
    fails in about a hundred milliseconds, while the identical URL connects
    perfectly from a shell, because a shell is not sandboxed.

    Pointing HOME at the app directory keeps the sandbox and gives asyncpg a
    path it can stat. Relaxing ProtectHome would also "work" and is the wrong
    trade.
    """
    environment = _environment(name)
    assert "HOME=" in environment, (
        f"{name}.service does not set HOME. With ProtectHome=true, asyncpg "
        f"raises PermissionError on $HOME/.postgresql/postgresql.key and every "
        f"database connection fails before opening a socket."
    )
    assert "HOME=/home" not in environment, (
        f"{name}.service points HOME back into /home, which ProtectHome makes "
        f"unreadable — the exact failure this setting exists to prevent."
    )


@pytest.mark.parametrize("name", UNITS)
def test_the_sandbox_is_not_quietly_relaxed(name: str) -> None:
    """ProtectHome is load-bearing: the secrets file and the CI key both live
    in directories it keeps out of reach."""
    unit = _unit(name)
    assert unit.get("Service", "ProtectHome", fallback="") == "true"
    assert unit.get("Service", "NoNewPrivileges", fallback="") == "true"
    assert unit.get("Service", "ProtectSystem", fallback="") == "strict"


@pytest.mark.parametrize("name", UNITS)
def test_the_service_runs_as_the_deploy_account(name: str) -> None:
    """Running as root re-owns /opt/als-backend and breaks every later deploy."""
    unit = _unit(name)
    assert unit.get("Service", "User", fallback="") == "als"


@pytest.mark.parametrize("name", UNITS)
def test_restart_is_unconditional(name: str) -> None:
    """
    ``deploy.sh`` falls back to signalling the process when it has no sudo rule,
    and that fallback is only safe if systemd brings the service back on its
    own. ``Restart=on-failure`` would not, since a signalled exit is clean.
    """
    unit = _unit(name)
    assert unit.get("Service", "Restart", fallback="") == "always"


def test_web_concurrency_has_a_default() -> None:
    """
    systemd expands ``${WEB_CONCURRENCY}`` literally. Unset, uvicorn is handed
    ``--workers`` with no value and exits before binding.
    """
    assert "WEB_CONCURRENCY=" in _environment("als-backend")


def test_both_units_read_the_same_secrets_file() -> None:
    """Two files would drift, and the drift is invisible until one of them is
    wrong in production."""
    paths = {
        _unit(name).get("Service", "EnvironmentFile", fallback="") for name in UNITS
    }
    assert paths == {"/etc/als-backend/env"}
