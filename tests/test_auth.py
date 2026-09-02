"""Tests de la délivrance et de la vérification des jetons (EV-12)."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from jose import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.user import LOCAL_PROVIDER, AppUser
from app.password import needs_rehash, verify_password
from tests.conftest import (
    FEDERATED_USERNAME,
    LEGACY_USERNAME,
    READER_USERNAME,
    SITE_WITH_READINGS,
    TEST_PASSWORD,
    TOKEN_URL,
    UNKNOWN_USERNAME,
    WRITER_USERNAME,
    assert_error_response,
    obtain_token,
    weak_hasher,
)

pytestmark = pytest.mark.anyio

SITES_URL = "/api/v1/sites"


def decode(token: str) -> dict:
    """Décode un jeton avec la clé de l'application."""
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def forge(claims: dict, secret: str | None = None) -> str:
    """Signe un jeu de claims arbitraire, pour fabriquer des jetons invalides."""
    settings = get_settings()
    return jwt.encode(
        claims,
        secret or settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


@pytest.mark.parametrize(
    ("username", "expected_role"),
    [(READER_USERNAME, "reader"), (WRITER_USERNAME, "writer")],
)
async def test_token_is_delivered_to_seeded_users(
    anonymous_client: AsyncClient,
    username: str,
    expected_role: str,
) -> None:
    response = await anonymous_client.post(
        TOKEN_URL,
        data={"username": username, "password": TEST_PASSWORD},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == get_settings().access_token_expire_minutes * 60
    assert decode(body["access_token"])["role"] == expected_role


async def test_token_claims_are_complete(anonymous_client: AsyncClient) -> None:
    """sub, role, exp et iat sont présents, et l'expiration suit la configuration."""
    before = datetime.now(UTC)
    token = await obtain_token(anonymous_client, READER_USERNAME)
    claims = decode(token)

    assert claims["sub"] == READER_USERNAME
    assert claims["role"] == "reader"

    issued_at = datetime.fromtimestamp(claims["iat"], UTC)
    expires_at = datetime.fromtimestamp(claims["exp"], UTC)
    # Une seconde de marge : la requête a pris un temps non nul.
    assert issued_at >= before - timedelta(seconds=1)
    lifetime = timedelta(minutes=get_settings().access_token_expire_minutes)
    assert expires_at - issued_at == lifetime


@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("mot de passe faux", {"username": READER_USERNAME, "password": "pas-le-bon"}),
        ("utilisateur inconnu", {"username": UNKNOWN_USERNAME, "password": TEST_PASSWORD}),
        # Une identité fédérée existe en base mais n'a pas de mot de passe
        # local : elle ne doit pas pouvoir passer par ce flux.
        ("identite federee", {"username": FEDERATED_USERNAME, "password": TEST_PASSWORD}),
    ],
)
async def test_token_refuses_bad_credentials(
    anonymous_client: AsyncClient,
    case: str,
    payload: dict,
) -> None:
    response = await anonymous_client.post(TOKEN_URL, data=payload)

    assert response.status_code == 401, case
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert_error_response(response.json())


async def test_token_message_does_not_reveal_whether_the_account_exists(
    anonymous_client: AsyncClient,
) -> None:
    """Compte inconnu et mot de passe faux répondent exactement la même chose.

    Un message différent permettrait d'énumérer les comptes existants.
    """
    wrong_password = await anonymous_client.post(
        TOKEN_URL,
        data={"username": READER_USERNAME, "password": "pas-le-bon"},
    )
    unknown_user = await anonymous_client.post(
        TOKEN_URL,
        data={"username": UNKNOWN_USERNAME, "password": TEST_PASSWORD},
    )

    assert wrong_password.json() == unknown_user.json()


@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("mot de passe absent", {"username": READER_USERNAME}),
        ("identifiant absent", {"password": TEST_PASSWORD}),
        ("formulaire vide", {}),
    ],
)
async def test_token_requires_a_complete_form(
    anonymous_client: AsyncClient,
    case: str,
    payload: dict,
) -> None:
    response = await anonymous_client.post(TOKEN_URL, data=payload)

    assert response.status_code == 422, case
    assert_error_response(response.json())


async def test_protected_route_accepts_a_valid_token(api_client: AsyncClient) -> None:
    """Les lectures d'EV-11 répondent 200 avec un jeton valide."""
    response = await api_client.get(SITES_URL)

    assert response.status_code == 200


async def test_protected_route_accepts_a_token_on_every_reading_endpoint(
    api_client: AsyncClient,
) -> None:
    latest = await api_client.get(
        f"{SITES_URL}/{SITE_WITH_READINGS}/readings/latest",
    )

    assert latest.status_code == 200


@pytest.mark.parametrize(
    ("case", "headers"),
    [
        ("en-tete absent", {}),
        ("schema inconnu", {"Authorization": "Basic dXNlcjpwYXNz"}),
        ("Bearer sans jeton", {"Authorization": "Bearer"}),
        ("Bearer vide", {"Authorization": "Bearer "}),
        ("jeton qui n'est pas un JWT", {"Authorization": "Bearer pas-un-jeton"}),
    ],
)
async def test_malformed_authorization_is_refused(
    anonymous_client: AsyncClient,
    case: str,
    headers: dict,
) -> None:
    response = await anonymous_client.get(SITES_URL, headers=headers)

    assert response.status_code == 401, case
    assert response.headers["WWW-Authenticate"] == "Bearer", case
    assert_error_response(response.json())


async def test_expired_token_is_refused(anonymous_client: AsyncClient) -> None:
    issued_at = datetime.now(UTC) - timedelta(hours=2)
    expired = forge(
        {
            "sub": READER_USERNAME,
            "role": "reader",
            "iat": issued_at,
            "exp": issued_at + timedelta(minutes=1),
        },
    )

    response = await anonymous_client.get(
        SITES_URL,
        headers={"Authorization": f"Bearer {expired}"},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_token_signed_with_another_key_is_refused(
    anonymous_client: AsyncClient,
) -> None:
    issued_at = datetime.now(UTC)
    forged = forge(
        {
            "sub": READER_USERNAME,
            "role": "reader",
            "iat": issued_at,
            "exp": issued_at + timedelta(minutes=60),
        },
        secret="une-autre-cle-de-signature-tout-aussi-longue",
    )

    response = await anonymous_client.get(
        SITES_URL,
        headers={"Authorization": f"Bearer {forged}"},
    )

    assert response.status_code == 401


async def stored_hash_of(session: AsyncSession, username: str) -> str:
    """Relit le hachage en base, hors de tout cache d'identité."""
    return await session.scalar(
        select(AppUser.password_hash).where(
            AppUser.oauth_provider == LOCAL_PROVIDER,
            AppUser.oauth_subject == username,
        ),
    )


async def test_login_rehashes_an_outdated_hash(
    anonymous_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Une connexion réussie remplace un hachage aux paramètres dépassés.

    C'est le seul instant où l'API détient le mot de passe en clair, donc le
    seul où le remplaçant peut être calculé.
    """
    # Le hachage faible est reposé ici plutôt que laissé au seed : le test
    # reste vrai s'il est rejoué sur une base déjà remise à niveau.
    await db_session.execute(
        update(AppUser)
        .where(
            AppUser.oauth_provider == LOCAL_PROVIDER,
            AppUser.oauth_subject == LEGACY_USERNAME,
        )
        .values(password_hash=weak_hasher.hash(TEST_PASSWORD)),
    )
    await db_session.commit()
    before = await stored_hash_of(db_session, LEGACY_USERNAME)
    assert needs_rehash(before) is True

    await obtain_token(anonymous_client, LEGACY_USERNAME)

    # Nouvelle transaction : celle du test ne verrait pas le commit de l'API.
    await db_session.rollback()
    after = await stored_hash_of(db_session, LEGACY_USERNAME)
    assert after != before
    assert needs_rehash(after) is False
    # Le compte reste utilisable avec le même mot de passe.
    assert verify_password(TEST_PASSWORD, after) is True


async def test_login_leaves_a_current_hash_untouched(
    anonymous_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Un hachage à jour n'est pas réécrit : pas d'écriture à chaque connexion."""
    before = await stored_hash_of(db_session, READER_USERNAME)

    await obtain_token(anonymous_client, READER_USERNAME)

    await db_session.rollback()
    assert await stored_hash_of(db_session, READER_USERNAME) == before


async def test_token_without_subject_is_refused(
    anonymous_client: AsyncClient,
) -> None:
    """Jeton correctement signé mais sans claim sub : refusé.

    Rien n'oblige un porteur de la clé à mettre un sub ; sans identité, il n'y
    a personne à charger, donc rien à autoriser.
    """
    issued_at = datetime.now(UTC)
    subjectless = forge(
        {
            "role": "writer",
            "iat": issued_at,
            "exp": issued_at + timedelta(minutes=60),
        },
    )

    response = await anonymous_client.get(
        SITES_URL,
        headers={"Authorization": f"Bearer {subjectless}"},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_token_of_a_deleted_user_is_refused(
    anonymous_client: AsyncClient,
) -> None:
    """Jeton bien signé dont le porteur n'existe pas en base : refusé.

    Le rôle est relu en base à chaque requête, un sub inventé ne suffit donc
    pas à passer, même signé avec la bonne clé.
    """
    issued_at = datetime.now(UTC)
    orphan = forge(
        {
            "sub": UNKNOWN_USERNAME,
            "role": "writer",
            "iat": issued_at,
            "exp": issued_at + timedelta(minutes=60),
        },
    )

    response = await anonymous_client.get(
        SITES_URL,
        headers={"Authorization": f"Bearer {orphan}"},
    )

    assert response.status_code == 401
