"""Test configuration for the bundled runtime."""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from simplifi_runtime import artifacts  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    """Keep the suite out of the real `$XDG_DATA_HOME`.

    Commands create the data directory as a matter of course, so without this
    every test run would leave a directory in the developer's home. Tests that
    are *about* the default location unset this deliberately.
    """
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, str(tmp_path_factory.mktemp("data")))
    monkeypatch.delenv(artifacts.ALLOW_UNSAFE_ENV, raising=False)
