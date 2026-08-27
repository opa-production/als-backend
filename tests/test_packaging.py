"""
What `pip install .` actually installs.

This is not academic. `packages = ["app"]` in pyproject.toml installed the
top-level package and none of its subpackages, so every deploy put a broken
`app` into site-packages. Nothing caught it for months: uvicorn and pytest both
put the working directory on sys.path, so the source tree shadowed the install
in exactly the two places anyone looks. It surfaced the first time something ran
from a different directory -- `scripts/create_admin.py`, on a production box --
as `ModuleNotFoundError: No module named 'app.db'`.

Nothing else in the suite would notice, because the suite is one of the things
being shadowed.
"""

import tomllib
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source_packages() -> set[str]:
    """Every importable package under app/, as dotted names."""
    found = set()
    for init in (ROOT / "app").rglob("__init__.py"):
        found.add(".".join(init.parent.relative_to(ROOT).parts))
    return found


def _configured_patterns() -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    setuptools = config.get("tool", {}).get("setuptools", {})

    # The literal list form is the bug this file exists to prevent: it names
    # packages explicitly and silently drops anything not listed.
    assert "packages" not in setuptools or isinstance(
        setuptools.get("packages"), dict
    ), (
        "pyproject.toml uses an explicit [tool.setuptools] packages list. That "
        "installs only the packages named and drops every subpackage, which "
        "the source tree then hides from you. Use "
        "[tool.setuptools.packages.find] with include = ['app*']."
    )

    find = setuptools.get("packages", {}).get("find", {})
    return find.get("include", [])


def test_every_subpackage_is_included() -> None:
    patterns = _configured_patterns()
    assert patterns, "no [tool.setuptools.packages.find] include patterns"

    missing = sorted(
        name
        for name in _source_packages()
        if not any(fnmatch(name, pattern) for pattern in patterns)
    )
    assert not missing, (
        f"these packages exist in the tree but no include pattern matches them, "
        f"so `pip install .` will leave them out: {missing}"
    )


def test_the_app_package_has_subpackages_worth_shipping() -> None:
    """
    Guards the guard. If app/ were ever flattened, the test above would pass
    vacuously and stop protecting anything.
    """
    packages = _source_packages()
    assert "app" in packages
    assert len(packages) > 5, f"expected a nested app package, found {packages}"
    for expected in ("app.db", "app.api", "app.services", "app.models"):
        assert expected in packages, f"{expected} is missing from the source tree"
