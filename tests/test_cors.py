"""Tests des en-têtes CORS (EV-62).

Le dashboard est servi depuis une autre origine que l'API : sans ces en-têtes,
le navigateur refuse ses requêtes avant même de les envoyer. Ces tests
vérifient que l'origine déclarée passe et qu'une autre est écartée.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import (
    CorsConfigurationError,
    check_cors_origins,
    get_cors_origins,
    get_settings,
)
from tests.conftest import ALLOWED_ORIGIN, REFUSED_ORIGIN

PREFLIGHT_HEADERS = {
    "Origin": ALLOWED_ORIGIN,
    "Access-Control-Request-Method": "GET",
    "Access-Control-Request-Headers": "authorization",
}


def test_preflight_is_accepted_for_an_allowed_origin(client: TestClient) -> None:
    """La requête préalable du navigateur reçoit l'autorisation attendue."""
    response = client.options("/api/v1/sites", headers=PREFLIGHT_HEADERS)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "GET" in response.headers["access-control-allow-methods"]
    # Le jeton voyage dans cet en-tête : sans lui, aucune requête authentifiée.
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_preflight_is_refused_for_another_origin(client: TestClient) -> None:
    """Une origine non déclarée n'obtient pas l'en-tête d'autorisation.

    Starlette répond 400 à une requête préalable dont l'origine est inconnue.
    Ce qui compte est l'absence d'`access-control-allow-origin` : c'est elle
    que le navigateur lit pour décider de bloquer.
    """
    response = client.options(
        "/api/v1/sites",
        headers={**PREFLIGHT_HEADERS, "Origin": REFUSED_ORIGIN},
    )

    assert "access-control-allow-origin" not in response.headers


def test_simple_request_from_another_origin_gets_no_cors_header(
    client: TestClient,
) -> None:
    """Une requête simple d'une origine inconnue est servie sans en-tête CORS.

    Le serveur répond, mais le navigateur refusera de livrer la réponse au
    script appelant faute d'autorisation.
    """
    response = client.get("/api/v1/health", headers={"Origin": REFUSED_ORIGIN})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_configured_origins_are_split_on_commas() -> None:
    get_settings.cache_clear()
    assert get_cors_origins() == [ALLOWED_ORIGIN]


def test_a_wildcard_origin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un joker fait échouer la validation, il n'est jamais accepté en silence."""
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")
    get_settings.cache_clear()
    try:
        with pytest.raises(CorsConfigurationError, match="joker"):
            check_cors_origins()
    finally:
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", ALLOWED_ORIGIN)
        get_settings.cache_clear()
