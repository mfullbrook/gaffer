import pytest

from gaffer import config


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    """Tests always use a fake keychain; reaching /usr/bin/security fails the test."""

    def refuse(argv):
        raise AssertionError(f"test reached the real Keychain: {argv[:2]}")

    monkeypatch.setattr(config, "_run_security", refuse)
