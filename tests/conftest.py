from pathlib import Path

import pytest

from gtm.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_directory=tmp_path / "data",
        grok_directory=tmp_path / "data/incoming/grok",
        linkedin_directory=tmp_path / "data/incoming/linkedin",
        config_directory=Path(__file__).parents[1] / "config",
    )
