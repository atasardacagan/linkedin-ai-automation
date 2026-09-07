from linkedin_automation.config import Settings


def test_default_timezone_and_safe_mode():
    config = Settings(_env_file=None)
    assert config.timezone == "Europe/Istanbul"
    assert config.dry_run is True
