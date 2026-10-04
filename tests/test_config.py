"""Unit tests for ServiceNowConfig credential loading (Task 8.2).

Covers Requirements 10.1, 10.2, 10.3, 11.2:
- env present => config loads;
- each missing/empty var => MissingCredential naming the variable only;
- repr(config) / str(password) never expose the secret value;
- timeout bounds (default 30, reject <1 and >60).
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from sentinelnow.errors import MissingCredential
from sentinelnow.instance import (
    ENV_INSTANCE,
    ENV_PASSWORD,
    ENV_USER,
    ServiceNowConfig,
)

SECRET = "sup3r-s3cret-pw"


@pytest.fixture
def set_env(monkeypatch: pytest.MonkeyPatch):
    def _set(instance: str | None, user: str | None, password: str | None) -> None:
        for name, value in (
            (ENV_INSTANCE, instance),
            (ENV_USER, user),
            (ENV_PASSWORD, password),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    return _set


def test_from_env_loads_when_all_present(set_env) -> None:
    set_env("https://dev12345.service-now.com", "admin", SECRET)
    config = ServiceNowConfig.from_env()
    assert config.instance_url == "https://dev12345.service-now.com"
    assert config.user == "admin"
    assert isinstance(config.password, SecretStr)
    assert config.password.get_secret_value() == SECRET
    assert config.timeout_seconds == 30.0  # Req 11.2 default


@pytest.mark.parametrize("missing", [ENV_INSTANCE, ENV_USER, ENV_PASSWORD])
def test_missing_var_raises_naming_only_the_variable(set_env, missing: str) -> None:
    values = {ENV_INSTANCE: "https://x.service-now.com", ENV_USER: "admin", ENV_PASSWORD: SECRET}
    values[missing] = None  # drop exactly one
    set_env(values[ENV_INSTANCE], values[ENV_USER], values[ENV_PASSWORD])

    with pytest.raises(MissingCredential) as exc:
        ServiceNowConfig.from_env()
    assert missing in str(exc.value)
    # The secret value must never leak through the error, even for other vars.
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize("empty", [ENV_INSTANCE, ENV_USER, ENV_PASSWORD])
@pytest.mark.parametrize("blank", ["", "   "])
def test_empty_or_whitespace_var_raises(set_env, empty: str, blank: str) -> None:
    values = {ENV_INSTANCE: "https://x.service-now.com", ENV_USER: "admin", ENV_PASSWORD: SECRET}
    values[empty] = blank
    set_env(values[ENV_INSTANCE], values[ENV_USER], values[ENV_PASSWORD])

    with pytest.raises(MissingCredential) as exc:
        ServiceNowConfig.from_env()
    assert empty in str(exc.value)


def test_password_never_appears_in_repr_or_str(set_env) -> None:
    set_env("https://x.service-now.com", "admin", SECRET)
    config = ServiceNowConfig.from_env()
    assert SECRET not in repr(config)
    assert SECRET not in str(config)
    # SecretStr masks on str(); only get_secret_value() exposes it.
    assert SECRET not in str(config.password)
    assert config.password.get_secret_value() == SECRET


def test_timeout_default_is_30() -> None:
    config = ServiceNowConfig(instance_url="https://x", user="u", password=SecretStr("p"))
    assert config.timeout_seconds == 30.0


@pytest.mark.parametrize("bad_timeout", [0.0, 0.5, 0.999, 60.001, 120.0, -5.0])
def test_timeout_out_of_bounds_rejected(bad_timeout: float) -> None:
    with pytest.raises(ValidationError):
        ServiceNowConfig(
            instance_url="https://x",
            user="u",
            password=SecretStr("p"),
            timeout_seconds=bad_timeout,
        )


@pytest.mark.parametrize("ok_timeout", [1.0, 30.0, 60.0])
def test_timeout_within_bounds_accepted(ok_timeout: float) -> None:
    config = ServiceNowConfig(
        instance_url="https://x",
        user="u",
        password=SecretStr("p"),
        timeout_seconds=ok_timeout,
    )
    assert config.timeout_seconds == ok_timeout
