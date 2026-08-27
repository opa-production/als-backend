"""
`load_env_file` in scripts/deploy.sh, exercised against a hostile settings file.

The deploy has to read /etc/als-backend/env before running migrations, and the
obvious way to do that -- `. "$ENV_FILE"` -- is wrong. Sourcing lets the shell
evaluate the file, and these values are not shell. A database password
containing a backtick becomes a command substitution, and the deploy dies with
`No such file or directory` naming a fragment of the password. systemd does not
evaluate the file either, so the service stays up while the deploy fails, which
makes the cause hard to see.

This runs the real function out of the real script, so it cannot drift from what
ships.
"""

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy.sh"

# Values chosen because each one has broken a shell-based loader somewhere.
CASES = {
    "ENVIRONMENT": "production",
    "DATABASE_URL": "postgresql://postgres:g2b`whoami`Pk@db.example.co:5432/postgres",
    "DOLLAR_SUB": "postgresql://u:pa$(id)ss@host:5432/db",
    "SPACES": "hello world with spaces",
    "QUOTED": "double quoted value",
    "SQUOTED": "single quoted value",
    "EXPORTED": "yes",
    "EQUALS": "a=b=c",
    "STAR": "*",
    "EMPTY": "",
}

ENV_FILE = textwrap.dedent(
    """\
    # a comment

    ENVIRONMENT=production
    DATABASE_URL=postgresql://postgres:g2b`whoami`Pk@db.example.co:5432/postgres
    DOLLAR_SUB=postgresql://u:pa$(id)ss@host:5432/db
    SPACES=hello world with spaces
    QUOTED="double quoted value"
    SQUOTED='single quoted value'
    export EXPORTED=yes
    EQUALS=a=b=c
    STAR=*
    EMPTY=
    NOT A SETTING LINE
    """
)

def _working_bash() -> str | None:
    """
    A bash that can run a script at a path this process can write.

    `shutil.which("bash")` is not enough on Windows: it finds WSL's bash.exe,
    which cannot execute a Windows path and fails with an execvpe error rather
    than anything about the script. Probing with a real temporary file is the
    only reliable check, so Git Bash is used where it exists and the tests skip
    where nothing suitable does. On CI this is just /bin/bash.
    """
    import tempfile

    candidates = [
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.sh"
        probe.write_text("printf ok\n", encoding="utf-8", newline="\n")
        for candidate in candidates:
            if not candidate or not Path(candidate).exists():
                continue
            try:
                done = subprocess.run(
                    [candidate, str(probe)], capture_output=True, text=True, timeout=30
                )
            except OSError:
                continue
            if done.returncode == 0 and done.stdout.strip() == "ok":
                return candidate
    return None


BASH = _working_bash()

pytestmark = pytest.mark.skipif(
    BASH is None, reason="needs a bash that can run a script at a local path"
)


def _extract_function() -> str:
    body = DEPLOY.read_text(encoding="utf-8")
    match = re.search(r"^load_env_file\(\) \{.*?^\}", body, re.MULTILINE | re.DOTALL)
    assert match, "load_env_file() is no longer in scripts/deploy.sh"
    return match.group(0)


def test_values_survive_verbatim(tmp_path: Path) -> None:
    env_path = tmp_path / "env"
    env_path.write_text(ENV_FILE, encoding="utf-8", newline="\n")

    # NUL-delimited so values containing newlines or spaces cannot confuse the
    # split, and `set -e` so an abort mid-loop fails loudly rather than silently
    # leaving later keys unset -- which is exactly how the `STAR=*` bug hid.
    script = tmp_path / "run.sh"
    script.write_text(
        "set -euo pipefail\n"
        + _extract_function()
        + '\nload_env_file "$1"\n'
        + "".join(f'printf \'%s\\0\' "${{{key}?unset}}"\n' for key in CASES),
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [BASH, str(script), str(env_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    # Each printf ends with a NUL, so the split leaves one empty trailing
    # field. Dropping it lets strict=True catch a short read rather than
    # silently pairing off whatever arrived.
    values = result.stdout.split("\0")[:-1]
    assert dict(zip(CASES, values, strict=True)) == CASES


def test_a_crlf_file_does_not_smuggle_carriage_returns(tmp_path: Path) -> None:
    """
    An operator editing the file on Windows leaves \\r on every line. Carried
    into DATABASE_URL that produces a hostname with a trailing carriage return,
    and a DNS failure nobody can see in the log.
    """
    env_path = tmp_path / "env"
    env_path.write_bytes(b"DATABASE_URL=postgresql://host/db\r\nOTHER=x\r\n")

    script = tmp_path / "run.sh"
    script.write_text(
        "set -euo pipefail\n"
        + _extract_function()
        + '\nload_env_file "$1"\nprintf \'%s\\0\' "$DATABASE_URL"\n',
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [BASH, str(script), str(env_path)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\0")[0] == "postgresql://host/db"


def test_the_deploy_never_sources_the_settings_file() -> None:
    """The regression itself: `. "$ENV_FILE"` must not come back."""
    body = DEPLOY.read_text(encoding="utf-8")
    assert '. "$ENV_FILE"' not in body, (
        "deploy.sh sources the settings file again. That lets the shell "
        "evaluate passwords containing backticks or $( — use load_env_file()."
    )
