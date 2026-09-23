"""console_auth.py in isolation: signing, expiry, constant-time login and csrf compare."""

from __future__ import annotations

import time

import pytest

from orca_gateway import console_auth
from orca_gateway.config import get_settings


@pytest.fixture(autouse=True)
def _console_secret(monkeypatch):
    monkeypatch.setenv("ORCA_CONSOLE_SECRET", "unit-test-secret")
    monkeypatch.setenv("ORCA_CONSOLE_SESSION_TTL_S", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_check_login_requires_exact_match():
    assert console_auth.check_login("unit-test-secret") is True
    assert console_auth.check_login("unit-test-secre") is False
    assert console_auth.check_login("") is False


def test_check_login_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ORCA_CONSOLE_SECRET", raising=False)
    get_settings.cache_clear()
    assert console_auth.check_login("anything") is False
    assert console_auth.check_login("") is False


class _FakeRequest:
    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies


def test_round_trips_a_session_and_recovers_its_csrf_token():
    cookie_value, csrf = console_auth.new_session_cookie_value()
    recovered = console_auth.read_session(_FakeRequest({console_auth.COOKIE_NAME: cookie_value}))
    assert recovered == csrf


def test_no_cookie_raises():
    with pytest.raises(console_auth.ConsoleAuthError):
        console_auth.read_session(_FakeRequest({}))


def test_tampered_cookie_is_rejected():
    cookie_value, _ = console_auth.new_session_cookie_value()
    with pytest.raises(console_auth.ConsoleAuthError):
        console_auth.read_session(_FakeRequest({console_auth.COOKIE_NAME: cookie_value + "x"}))


def test_expired_cookie_is_rejected():
    cookie_value, _ = console_auth.new_session_cookie_value()
    time.sleep(2.1)  # itsdangerous truncates to whole seconds; stay well clear of the 1s ttl
    with pytest.raises(console_auth.ConsoleAuthError):
        console_auth.read_session(_FakeRequest({console_auth.COOKIE_NAME: cookie_value}))
