import tomllib
from pathlib import Path

from linkedin_automation import __version__
from linkedin_automation.main import app


def test_package_api_and_project_versions_match():
    project = tomllib.loads(Path("pyproject.toml").read_text())

    assert project["project"]["version"] == __version__
    assert app.version == __version__
