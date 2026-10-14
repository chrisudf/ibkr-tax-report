import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _isolated_app(tmp_path, monkeypatch):
    """Each test gets its own results directory (never the shared system temp
    one a local `python app.py` uses) and starts with rate limiting off."""
    import app as app_module
    monkeypatch.setattr(app_module.SETTINGS, "results_dir", str(tmp_path / "results"))
    monkeypatch.setattr(app_module.SETTINGS, "rate_limits", [])
    app_module.limiter.reset()
    yield
    app_module.stop_background()
