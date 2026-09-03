"""Tests de la validation de la clé de signature (EV-12).

Une API qui démarre sans clé utilisable répond, délivre des jetons et les
accepte, tout en étant forgeable. Ces tests figent le refus de démarrer.
"""

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import (
    JWT_SECRET_MIN_LENGTH,
    JwtSecretError,
    get_jwt_secret,
    get_settings,
)
from app.main import app


@pytest.fixture
def jwt_secret_env() -> Iterator[None]:
    """Rend JWT_SECRET modifiable puis le restaure, cache des réglages compris.

    monkeypatch ne suffit pas : le cache de get_settings doit être vidé après
    la restauration de la variable, pas avant, sinon il se reconstruirait sur
    la valeur du test et fuiterait vers les suivants.
    """
    original = os.environ.get("JWT_SECRET")
    get_settings.cache_clear()
    yield
    if original is None:
        os.environ.pop("JWT_SECRET", None)
    else:
        os.environ["JWT_SECRET"] = original
    get_settings.cache_clear()


def set_secret(value: str) -> None:
    os.environ["JWT_SECRET"] = value
    get_settings.cache_clear()


def test_secret_of_sufficient_length_is_accepted(jwt_secret_env: None) -> None:
    secret = "x" * JWT_SECRET_MIN_LENGTH
    set_secret(secret)

    assert get_jwt_secret() == secret


def test_missing_secret_is_rejected(jwt_secret_env: None) -> None:
    set_secret("")

    with pytest.raises(JwtSecretError, match="absent"):
        get_jwt_secret()


def test_short_secret_is_rejected(jwt_secret_env: None) -> None:
    set_secret("x" * (JWT_SECRET_MIN_LENGTH - 1))

    with pytest.raises(JwtSecretError, match="au minimum"):
        get_jwt_secret()


def test_application_refuses_to_start_without_a_usable_secret(
    jwt_secret_env: None,
) -> None:
    """Le refus a lieu au démarrage, avant d'accepter la moindre requête."""
    set_secret("trop-court")

    with pytest.raises(JwtSecretError), TestClient(app):
        pass


def test_application_starts_with_a_usable_secret(jwt_secret_env: None) -> None:
    set_secret("x" * JWT_SECRET_MIN_LENGTH)

    with TestClient(app) as started:
        assert started.get("/api/v1/health").status_code == 200
