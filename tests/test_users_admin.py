"""Tests de la gestion des comptes (EV-55).

Ces routes sont les seules de l'API dont la **lecture** est réservée : la liste
des comptes dit qui a accès à la plateforme, un `reader` n'a pas à la
connaître. Les tests le vérifient explicitement, sans quoi une ouverture
accidentelle passerait inaperçue.

L'essentiel du fichier porte sur les trois garde-fous, parce que c'est là que
se joue la différence entre une API d'administration et une API qui se rend
inadministrable.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import LOCAL_PROVIDER, AppUser
from app.password import hash_password, verify_password
from tests.conftest import (
    FEDERATED_USERNAME,
    LEGACY_USERNAME,
    READER_USERNAME,
    TEST_PASSWORD,
    WRITER_USERNAME,
    assert_error_response,
)

pytestmark = pytest.mark.anyio

USERS_URL = "/api/v1/users"

# Assez long pour la contrainte du DTO, et manifestement de test.
NEW_PASSWORD = "mot-de-passe-de-test-1"

# Comptes posés par le seed, qui doivent survivre à chaque test de ce fichier.
SEEDED = frozenset(
    {READER_USERNAME, WRITER_USERNAME, FEDERATED_USERNAME, LEGACY_USERNAME},
)


@pytest.fixture(autouse=True)
async def restored_accounts(db_session: AsyncSession):
    """Rend au jeu de comptes l'état que le seed lui a donné.

    La base de test est peuplée **une fois pour la session** : la conftest le
    dit, et c'était vrai tant qu'aucun test n'écrivait. Ceux-ci écrivent, et
    trois d'entre eux se contamineraient sans cette restauration :

    - réinitialiser le mot de passe de `dev.reader` empêche les tests suivants
      d'obtenir un jeton, puisque `obtain_token` passe par le vrai endpoint ;
    - promouvoir un second `writer` fait mentir le garde-fou du dernier
      writer, qui compte les comptes ;
    - rétrograder `dev.writer` prive les tests suivants du rôle nécessaire
      pour appeler ces routes.

    Le nettoyage a lieu **après** chaque test plutôt qu'avant : un échec laisse
    alors la base dans un état propre pour les fichiers suivants, et non dans
    celui qui a fait échouer.
    """
    yield

    await db_session.execute(
        delete(AppUser).where(AppUser.oauth_subject.not_in(SEEDED)),
    )
    # Rôles et mots de passe remis à leur valeur d'origine. Le hachage est
    # recalculé plutôt que mémorisé : argon2 sale chaque empreinte, donc deux
    # hachages du même mot de passe diffèrent, et seul le mot de passe compte.
    await db_session.execute(
        update(AppUser)
        .where(AppUser.oauth_subject == READER_USERNAME)
        .values(role="reader", password_hash=hash_password(TEST_PASSWORD)),
    )
    await db_session.execute(
        update(AppUser)
        .where(AppUser.oauth_subject == WRITER_USERNAME)
        .values(role="writer", password_hash=hash_password(TEST_PASSWORD)),
    )
    await db_session.commit()


async def _find(session: AsyncSession, username: str) -> AppUser | None:
    return await session.scalar(
        select(AppUser).where(
            AppUser.oauth_provider == LOCAL_PROVIDER,
            AppUser.oauth_subject == username,
        ),
    )


class TestAccess:
    async def test_anonyme_refuse(self, anonymous_client: AsyncClient) -> None:
        response = await anonymous_client.get(USERS_URL)

        assert response.status_code == 401
        assert_error_response(response.json())

    async def test_reader_refuse_meme_en_lecture(self, api_client: AsyncClient) -> None:
        # La liste des comptes n'est pas une donnée d'exploitation : c'est la
        # seule lecture réservée de l'API, et c'est délibéré.
        response = await api_client.get(USERS_URL)

        assert response.status_code == 403
        assert_error_response(response.json())

    async def test_writer_autorise(self, writer_client: AsyncClient) -> None:
        response = await writer_client.get(USERS_URL)

        assert response.status_code == 200


class TestList:
    async def test_publie_les_comptes_locaux(self, writer_client: AsyncClient) -> None:
        response = await writer_client.get(USERS_URL)

        payload = response.json()
        usernames = [row["username"] for row in payload]
        assert READER_USERNAME in usernames
        assert WRITER_USERNAME in usernames

    async def test_ecarte_les_identites_federees(self, writer_client: AsyncClient) -> None:
        # Ces routes ne savent pas les gérer : les afficher promettrait une
        # action impossible.
        response = await writer_client.get(USERS_URL)

        usernames = [row["username"] for row in response.json()]
        assert FEDERATED_USERNAME not in usernames

    async def test_ne_publie_jamais_le_hachage(self, writer_client: AsyncClient) -> None:
        response = await writer_client.get(USERS_URL)

        for row in response.json():
            assert "password" not in row
            assert "password_hash" not in row

    async def test_ordonne_par_identifiant(self, writer_client: AsyncClient) -> None:
        # L'ordre de création ne bouge pas quand un compte change de rôle : les
        # lignes ne sautent pas d'un rafraîchissement à l'autre.
        response = await writer_client.get(USERS_URL)

        ids = [row["user_id"] for row in response.json()]
        assert ids == sorted(ids)


class TestCreate:
    async def test_cree_un_compte_utilisable(
        self,
        writer_client: AsyncClient,
        anonymous_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        response = await writer_client.post(
            USERS_URL,
            json={
                "username": "recette.pilote",
                "email": "recette.pilote@enervision.local",
                "display_name": "Recette pilote",
                "role": "reader",
                "password": NEW_PASSWORD,
            },
        )

        assert response.status_code == 201, response.text
        created = response.json()
        assert created["username"] == "recette.pilote"
        assert created["role"] == "reader"
        assert created["can_sign_in"] is True
        assert created["last_login_at"] is None

        # Le compte doit pouvoir se connecter : c'est tout l'objet de sa
        # création, et c'est ce qui prouve que le hachage est exploitable.
        token_response = await anonymous_client.post(
            "/api/v1/auth/token",
            data={"username": "recette.pilote", "password": NEW_PASSWORD},
        )
        assert token_response.status_code == 200, token_response.text

        stored = await _find(db_session, "recette.pilote")
        assert stored is not None
        # Haché, jamais en clair.
        assert stored.password_hash is not None
        assert stored.password_hash != NEW_PASSWORD
        assert verify_password(NEW_PASSWORD, stored.password_hash)

    async def test_reader_par_defaut(self, writer_client: AsyncClient) -> None:
        # Un compte reçoit le droit d'écrire parce qu'on le lui donne, jamais
        # par omission.
        response = await writer_client.post(
            USERS_URL,
            json={
                "username": "sans.role",
                "email": "sans.role@enervision.local",
                "password": NEW_PASSWORD,
            },
        )

        assert response.status_code == 201, response.text
        assert response.json()["role"] == "reader"

    async def test_identifiant_deja_pris(self, writer_client: AsyncClient) -> None:
        response = await writer_client.post(
            USERS_URL,
            json={
                "username": READER_USERNAME,
                "email": "doublon@enervision.local",
                "password": NEW_PASSWORD,
            },
        )

        # 409 et non 500 : la contrainte de la base refuserait l'insertion,
        # mais par une erreur d'intégrité que le client ne saurait pas lire.
        assert response.status_code == 409
        assert_error_response(response.json())

    async def test_mot_de_passe_trop_court(self, writer_client: AsyncClient) -> None:
        response = await writer_client.post(
            USERS_URL,
            json={
                "username": "trop.court",
                "email": "trop.court@enervision.local",
                "password": "court",
            },
        )

        assert response.status_code == 422
        assert_error_response(response.json())

    async def test_adresse_invalide(self, writer_client: AsyncClient) -> None:
        response = await writer_client.post(
            USERS_URL,
            json={
                "username": "sans.arobase",
                "email": "pas-une-adresse",
                "password": NEW_PASSWORD,
            },
        )

        assert response.status_code == 422

    async def test_identifiant_avec_espace(self, writer_client: AsyncClient) -> None:
        # Deux comptes indiscernables à l'œil dans un journal seraient pires
        # qu'un identifiant refusé.
        response = await writer_client.post(
            USERS_URL,
            json={
                "username": "avec espace",
                "email": "avec.espace@enervision.local",
                "password": NEW_PASSWORD,
            },
        )

        assert response.status_code == 422

    async def test_champ_inconnu_refuse(self, writer_client: AsyncClient) -> None:
        # Une faute de frappe sur un nom de champ ne doit pas se solder par une
        # création silencieusement incomplète.
        response = await writer_client.post(
            USERS_URL,
            json={
                "username": "champ.inconnu",
                "email": "champ.inconnu@enervision.local",
                "password": NEW_PASSWORD,
                "roles": "writer",
            },
        )

        assert response.status_code == 422


class TestUpdate:
    async def test_change_le_role(
        self,
        writer_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        reader = await _find(db_session, READER_USERNAME)
        assert reader is not None

        response = await writer_client.patch(
            f"{USERS_URL}/{reader.user_id}",
            json={"role": "writer"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["role"] == "writer"

    async def test_ne_touche_pas_aux_champs_absents(
        self,
        writer_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        reader = await _find(db_session, READER_USERNAME)
        assert reader is not None
        avant = reader.email

        response = await writer_client.patch(
            f"{USERS_URL}/{reader.user_id}",
            json={"display_name": "Lecteur de recette"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["display_name"] == "Lecteur de recette"
        # Une requête ne portant que le nom affiché ne doit pas effacer
        # l'adresse.
        assert response.json()["email"] == avant

    async def test_reinitialise_le_mot_de_passe(
        self,
        writer_client: AsyncClient,
        anonymous_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        reader = await _find(db_session, READER_USERNAME)
        assert reader is not None

        response = await writer_client.patch(
            f"{USERS_URL}/{reader.user_id}",
            json={"password": NEW_PASSWORD},
        )
        assert response.status_code == 200, response.text

        # Le nouveau mot de passe ouvre la session, l'ancien ne l'ouvre plus.
        nouveau = await anonymous_client.post(
            "/api/v1/auth/token",
            data={"username": READER_USERNAME, "password": NEW_PASSWORD},
        )
        assert nouveau.status_code == 200, nouveau.text

        ancien = await anonymous_client.post(
            "/api/v1/auth/token",
            data={"username": READER_USERNAME, "password": TEST_PASSWORD},
        )
        assert ancien.status_code == 401

    async def test_compte_inconnu(self, writer_client: AsyncClient) -> None:
        response = await writer_client.patch(f"{USERS_URL}/999999", json={"role": "writer"})

        assert response.status_code == 404
        assert_error_response(response.json())

    async def test_reader_refuse(self, api_client: AsyncClient) -> None:
        response = await api_client.patch(f"{USERS_URL}/1", json={"role": "writer"})

        assert response.status_code == 403


class TestGardeFous:
    async def test_refuse_de_retrograder_le_dernier_writer(
        self,
        writer_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        writer = await _find(db_session, WRITER_USERNAME)
        assert writer is not None

        response = await writer_client.patch(
            f"{USERS_URL}/{writer.user_id}",
            json={"role": "reader"},
        )

        # Plus personne ne pourrait alors gérer les comptes, et la seule issue
        # serait un UPDATE à la main en base.
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "dernier compte writer" in detail

    async def test_retrograde_un_writer_s_il_en_reste_un(
        self,
        writer_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Un second writer, et la rétrogradation redevient possible.
        created = await writer_client.post(
            USERS_URL,
            json={
                "username": "second.writer",
                "email": "second.writer@enervision.local",
                "role": "writer",
                "password": NEW_PASSWORD,
            },
        )
        assert created.status_code == 201, created.text

        writer = await _find(db_session, WRITER_USERNAME)
        assert writer is not None
        response = await writer_client.patch(
            f"{USERS_URL}/{writer.user_id}",
            json={"role": "reader"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["role"] == "reader"

    async def test_refuse_l_auto_suppression(
        self,
        writer_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        writer = await _find(db_session, WRITER_USERNAME)
        assert writer is not None

        response = await writer_client.delete(f"{USERS_URL}/{writer.user_id}")

        # Se supprimer, c'est se déconnecter en croyant faire le ménage.
        assert response.status_code == 409
        assert "lui-même" in response.json()["detail"]

    async def test_refuse_de_supprimer_le_dernier_writer(
        self,
        anonymous_client: AsyncClient,
        db_session: AsyncSession,
        auth_disabled: None,
    ) -> None:
        # Ce garde-fou n'est atteignable que sans authentification, et c'est
        # une propriété de l'API, pas une facilité de test : pour supprimer le
        # dernier writer en étant authentifié, il faudrait être writer sans
        # être ce dernier writer — donc être un second writer, ce qui le rend
        # aussitôt non-dernier. Le refus d'auto-suppression répond alors
        # toujours en premier, comme le vérifie le test précédent.
        #
        # AUTH_ENABLED à false, aucun appelant n'est identifié : le contrôle
        # d'auto-suppression est sans objet, et le compte du dernier writer
        # devient une cible atteignable.
        writer = await _find(db_session, WRITER_USERNAME)
        assert writer is not None

        response = await anonymous_client.delete(f"{USERS_URL}/{writer.user_id}")

        assert response.status_code == 409
        assert "dernier compte writer" in response.json()["detail"]

    async def test_supprime_un_writer_s_il_en_reste_un(
        self,
        writer_client: AsyncClient,
    ) -> None:
        # Le pendant du refus : dès qu'un second writer existe, il est
        # supprimable — ce n'est pas le rôle qui protège, c'est le fait d'être
        # le dernier.
        created = await writer_client.post(
            USERS_URL,
            json={
                "username": "ephemere.writer",
                "email": "ephemere.writer@enervision.local",
                "role": "writer",
                "password": NEW_PASSWORD,
            },
        )
        assert created.status_code == 201, created.text

        response = await writer_client.delete(f"{USERS_URL}/{created.json()['user_id']}")

        assert response.status_code == 204, response.text


class TestDelete:
    async def test_supprime_un_reader(
        self,
        writer_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        created = await writer_client.post(
            USERS_URL,
            json={
                "username": "a.supprimer",
                "email": "a.supprimer@enervision.local",
                "password": NEW_PASSWORD,
            },
        )
        assert created.status_code == 201, created.text

        response = await writer_client.delete(f"{USERS_URL}/{created.json()['user_id']}")

        assert response.status_code == 204
        assert response.content == b""
        assert await _find(db_session, "a.supprimer") is None

    async def test_compte_inconnu(self, writer_client: AsyncClient) -> None:
        response = await writer_client.delete(f"{USERS_URL}/999999")

        assert response.status_code == 404

    async def test_reader_refuse(self, api_client: AsyncClient) -> None:
        response = await api_client.delete(f"{USERS_URL}/1")

        assert response.status_code == 403
