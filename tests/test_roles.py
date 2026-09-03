"""Tests de get_current_user et de require_role (EV-12).

Ces deux dépendances ne sont observables qu'à travers un endpoint, et le
contrat gelé n'en publie aucun qui les expose : pas de /auth/me, aucune route
d'écriture. Les tests montent donc leurs propres routes dans une application
FastAPI distincte, jamais servie ni exportée. En ajouter à l'application
livrée ferait dériver la spécification, ce que la règle numéro 1 interdit.

require_role n'est encore câblée sur aucun endpoint réel : elle est livrée
prête pour les futures écritures, et testée ici pour ne pas l'être le jour où
elle servira.
"""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import get_db
from app.schemas.auth import UserOut
from app.security import CurrentUser, require_role
from tests.conftest import (
    READER_USERNAME,
    WRITER_USERNAME,
    assert_error_response,
    obtain_token,
)

pytestmark = pytest.mark.anyio

WritersOnly = Annotated[UserOut | None, Depends(require_role("writer"))]


def build_probe_app() -> FastAPI:
    """Application de sondage : deux routes, hors contrat et hors spécification."""
    probe = FastAPI()

    @probe.get("/who-am-i")
    async def who_am_i(user: CurrentUser) -> dict:
        return {"user": None if user is None else user.model_dump()}

    @probe.get("/writers-only")
    async def writers_only(user: WritersOnly) -> dict:
        return {"granted_to": None if user is None else user.username}

    return probe


@pytest.fixture
async def probe_client(engine, seeded_database) -> AsyncClient:
    """Client HTTP sur l'application de sondage, branché sur la base de test."""
    probe = build_probe_app()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    probe.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=probe)
    async with AsyncClient(transport=transport, base_url="http://probe") as http_client:
        yield http_client


@pytest.mark.parametrize(
    ("username", "expected_role"),
    [(READER_USERNAME, "reader"), (WRITER_USERNAME, "writer")],
)
async def test_current_user_exposes_username_and_role(
    anonymous_client: AsyncClient,
    probe_client: AsyncClient,
    username: str,
    expected_role: str,
) -> None:
    """get_current_user rend l'identité et le rôle portés par le jeton."""
    token = await obtain_token(anonymous_client, username)

    response = await probe_client.get(
        "/who-am-i",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["user"] == {"username": username, "role": expected_role}


async def test_require_role_admits_the_expected_role(
    anonymous_client: AsyncClient,
    probe_client: AsyncClient,
) -> None:
    token = await obtain_token(anonymous_client, WRITER_USERNAME)

    response = await probe_client.get(
        "/writers-only",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"granted_to": WRITER_USERNAME}


async def test_require_role_refuses_an_insufficient_role(
    anonymous_client: AsyncClient,
    probe_client: AsyncClient,
) -> None:
    """Un reader est refusé en 403, pas en 401 : il est authentifié, pas autorisé."""
    token = await obtain_token(anonymous_client, READER_USERNAME)

    response = await probe_client.get(
        "/writers-only",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert_error_response(response.json())


async def test_require_role_refuses_an_anonymous_call(
    probe_client: AsyncClient,
) -> None:
    """Sans jeton, le refus vient de get_current_user, donc en 401."""
    response = await probe_client.get("/writers-only")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_role_check_is_bypassed_when_auth_is_disabled(
    probe_client: AsyncClient,
    auth_disabled: None,
) -> None:
    """AUTH_ENABLED à false : aucun utilisateur identifiable, donc aucun contrôle.

    L'appliquer quand même rendrait toute écriture impossible en développement
    local, où personne ne se connecte.
    """
    response = await probe_client.get("/writers-only")

    assert response.status_code == 200
    assert response.json() == {"granted_to": None}
