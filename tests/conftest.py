from pathlib import Path

import pytest

from gtm.config import Settings
from gtm.service import GTMService
from gtm.state import State
from gtm.store import CSVStore


@pytest.fixture
def system(tmp_path):
    settings = Settings(
        tracker=tmp_path / "data/prospects.csv",
        database=tmp_path / "data/state/local.sqlite",
        data_directory=tmp_path / "data",
        grok_directory=tmp_path / "data/incoming/grok",
        linkedin_directory=tmp_path / "data/incoming/linkedin",
        config_directory=Path(__file__).parents[1] / "config",
        stability_seconds=0.1,
    )
    state = State(settings.database)
    service = GTMService(CSVStore(settings.tracker), state, settings.rules())
    yield service, settings
    state.close()
