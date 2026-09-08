"""Tests du job de rafraîchissement des prédictions (EV-38).

Serving est remplacé par un `httpx.MockTransport` : le délai dépassé se
provoque sans service en écoute, et l'on peut compter exactement combien de
sites ont été appelés. La base reste réelle, c'est elle qui porte la clé
d'idempotence.
"""

from collections.abc import Callable, Iterator
from datetime import datetime

import asyncpg
import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.jobs import predict_refresh
from app.models.prediction import Modele, Prediction
from app.predict_client import (
    ServingNotReadyError,
    ServingUnavailableError,
    request_prediction,
)
from tests.conftest import (
    AMBIGUOUS_MODEL_VERSION,
    EXTRA_ACTIVE_SITE_IDS,
    KNOWN_MODEL_VERSION,
    SITE_WITH_READINGS,
    SITE_WITHOUT_READINGS,
    UNKNOWN_MODEL_VERSION,
)

pytestmark = pytest.mark.anyio

SERVING_BASE_URL = "http://serving-de-test:8000"
GENERATED_AT = "2026-09-03T08:00:00Z"
# Version présente au registre : c'est elle qui permet de rattacher la
# prédiction à modele_id, que la table exige.
MODEL_VERSION = KNOWN_MODEL_VERSION

# Deux points par site : assez pour vérifier qu'une ligne est écrite par point
# sans alourdir chaque exécution du job.
POINTS = [
    {
        "timestamp": "2026-09-03T09:00:00Z",
        "predicted_consumption_kw": 131.5,
        "lower_bound_kw": 120.0,
        "upper_bound_kw": 143.0,
    },
    {
        "timestamp": "2026-09-03T10:00:00Z",
        "predicted_consumption_kw": 128.25,
        "lower_bound_kw": None,
        "upper_bound_kw": None,
    },
]

# Référentiel complet des tests avec extra_active_sites : les sept sites du
# projet, SITE003 étant inactif depuis EV-11. Le job ne doit voir que les six
# actifs.
SEEDED_ACTIVE = (SITE_WITH_READINGS, "SITE002")
ALL_ACTIVE = (*SEEDED_ACTIVE, *EXTRA_ACTIVE_SITE_IDS)
REFERENTIAL_SIZE = len(ALL_ACTIVE) + 1  # les six actifs, plus SITE003 inactif


def prediction_for(site_id: str) -> dict:
    return {
        "site_id": site_id,
        "model_version": MODEL_VERSION,
        "generated_at": GENERATED_AT,
        "points": POINTS,
    }


class ServingDouble:
    """Serving de test : répond par site et retient les sites appelés."""

    def __init__(self) -> None:
        self.called_sites: list[str] = []
        self.failing: set[str] = set()
        self.fail_all = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        import json

        site_id = json.loads(request.content)["site_id"]
        self.called_sites.append(site_id)
        if self.fail_all or site_id in self.failing:
            raise httpx.ReadTimeout("delai depasse", request=request)
        return httpx.Response(200, json=prediction_for(site_id))


@pytest.fixture
def serving(
    monkeypatch: pytest.MonkeyPatch,
    engine,
    seeded_database,
) -> Iterator[ServingDouble]:
    """Substitue le double à Serving et branche le job sur la base de test.

    Le job ouvre ses propres sessions par get_session_factory, qui lit
    DATABASE_URL : sans cette substitution il irait écrire dans la base de
    développement au lieu de celle des tests.
    """
    double = ServingDouble()

    def build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(double.handle))

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.predict_client.build_client", build_client)
    monkeypatch.setattr(predict_refresh, "get_session_factory", lambda: factory)
    monkeypatch.setenv("PREDICT_URL", SERVING_BASE_URL)
    get_settings.cache_clear()
    yield double
    monkeypatch.delenv("PREDICT_URL", raising=False)
    get_settings.cache_clear()


@pytest.fixture
async def clean_predictions(db_session: AsyncSession) -> AsyncSession:
    """Vide la table prediction avant le test."""
    await db_session.execute(delete(Prediction))
    await db_session.commit()
    return db_session


async def count_predictions(session: AsyncSession, site_id: str | None = None) -> int:
    await session.rollback()
    statement = select(func.count()).select_from(Prediction)
    if site_id is not None:
        statement = statement.where(Prediction.site_id == site_id)
    return await session.scalar(statement)


async def test_job_predicts_every_active_site(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    extra_active_sites: tuple[str, ...],
) -> None:
    """Sur les sept sites du référentiel, les six actifs sont prédits.

    Une ligne est archivée par point et par site.
    """
    code = await predict_refresh.refresh_all()

    assert code == 0
    assert REFERENTIAL_SIZE == 7
    assert sorted(serving.called_sites) == sorted(ALL_ACTIVE)
    assert len(ALL_ACTIVE) == 6
    assert await count_predictions(clean_predictions) == 6 * len(POINTS)


async def test_job_ignores_inactive_sites(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
) -> None:
    """Un site inactif n'est ni appelé ni archivé."""
    await predict_refresh.refresh_all()

    assert SITE_WITHOUT_READINGS not in serving.called_sites
    assert await count_predictions(clean_predictions, SITE_WITHOUT_READINGS) == 0


async def test_job_stores_the_contract_fields(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    known_model_id: int,
) -> None:
    """Les six champs du contrat sont restituables, bornes nulles comprises.

    model_version n'est pas archivé : il vient de modele.version par la
    jointure sur modele_id, que le job renseigne.
    """
    await predict_refresh.refresh_all()

    await clean_predictions.rollback()
    rows = await clean_predictions.execute(
        select(
            Prediction.ts_cible,
            Prediction.consumption_kw_predite,
            Prediction.lower_bound_kw,
            Prediction.upper_bound_kw,
            Modele.version,
            Prediction.generated_at,
            Prediction.modele_id,
        )
        .join(Modele, Modele.modele_id == Prediction.modele_id)
        .where(Prediction.site_id == SITE_WITH_READINGS)
        .order_by(Prediction.ts_cible),
    )
    stored = list(rows)

    assert len(stored) == 2
    first, second = stored
    assert float(first[1]) == 131.5
    assert float(first[2]) == 120.0
    assert float(first[3]) == 143.0
    # La version relue par jointure est celle que le service a annoncée.
    assert first[4] == MODEL_VERSION
    assert first[5].isoformat().startswith("2026-09-03T08:00:00")
    assert first[6] == known_model_id
    # Le point sans intervalle de confiance garde ses nulls.
    assert second[2] is None
    assert second[3] is None


async def test_one_failing_site_does_not_stop_the_others(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    extra_active_sites: tuple[str, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un site en délai dépassé est journalisé, les cinq autres sont archivés.

    Le code de sortie reste 0 : une panne isolée ne doit pas réveiller
    l'astreinte.
    """
    serving.failing = {SITE_WITH_READINGS}

    with caplog.at_level("ERROR", logger="app.jobs.predict_refresh"):
        code = await predict_refresh.refresh_all()

    assert code == 0
    assert await count_predictions(clean_predictions, SITE_WITH_READINGS) == 0
    remaining = len(ALL_ACTIVE) - 1
    assert await count_predictions(clean_predictions) == remaining * len(POINTS)
    assert SITE_WITH_READINGS in caplog.text
    assert "indisponible" in caplog.text


async def test_all_sites_failing_returns_non_zero(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Panne générale : code de sortie non nul, pour que le cron alerte."""
    serving.fail_all = True

    with caplog.at_level("ERROR", logger="app.jobs.predict_refresh"):
        code = await predict_refresh.refresh_all()

    assert code == 1
    assert await count_predictions(clean_predictions) == 0
    assert "Tous les sites ont échoué" in caplog.text


async def test_job_is_idempotent(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
) -> None:
    """Rejoué sur la même réponse de Serving, le job ne duplique rien.

    La clé d'unicité est (site_id, ts_cible, generated_at) et les conflits sont
    ignorés : la seconde exécution n'insère aucune ligne.
    """
    first_code = await predict_refresh.refresh_all()
    after_first = await count_predictions(clean_predictions)

    second_code = await predict_refresh.refresh_all()

    assert first_code == second_code == 0
    assert await count_predictions(clean_predictions) == after_first


async def test_new_generation_replaces_the_previous_one(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une nouvelle prévision du même instant remplace la précédente.

    La clé du schéma v1.0 est (modele_id, site_id, ts_cible) : il n'y a qu'une
    ligne par instant cible et par modèle, et c'est la plus fraîche qui reste.
    generated_at est mis à jour avec elle, sans quoi la ligne porterait la date
    de production de la prévision remplacée.
    """
    await predict_refresh.refresh_all()
    after_first = await count_predictions(clean_predictions)

    revised = [
        {**POINTS[0], "predicted_consumption_kw": 140.0},
        *POINTS[1:],
    ]

    def build_client() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            site_id = json.loads(request.content)["site_id"]
            serving.called_sites.append(site_id)
            return httpx.Response(
                200,
                json={
                    **prediction_for(site_id),
                    "generated_at": "2026-09-03T09:00:00Z",
                    "points": revised,
                },
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.predict_client.build_client", build_client)
    await predict_refresh.refresh_all()

    # Aucune ligne de plus : les prévisions se remplacent.
    assert await count_predictions(clean_predictions) == after_first

    await clean_predictions.rollback()
    row = (
        await clean_predictions.execute(
            select(Prediction.consumption_kw_predite, Prediction.generated_at)
            .where(
                Prediction.site_id == SITE_WITH_READINGS,
                Prediction.ts_cible == datetime.fromisoformat(POINTS[0]["timestamp"]),
            ),
        )
    ).one()
    assert float(row[0]) == 140.0
    assert row[1].isoformat().startswith("2026-09-03T09:00:00")


@pytest.mark.parametrize(
    ("case", "handler"),
    [
        ("erreur 500", lambda request: httpx.Response(500, text="boom")),
        ("reponse hors contrat", lambda request: httpx.Response(200, json={"x": 1})),
        ("reponse illisible", lambda request: httpx.Response(200, text="pas du json")),
    ],
)
async def test_unusable_serving_answers_are_failures(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Une réponse inexploitable compte comme une panne, rien n'est archivé."""

    def build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.predict_client.build_client", build_client)

    code = await predict_refresh.refresh_all()

    assert code == 1, case
    assert await count_predictions(clean_predictions) == 0, case


async def test_missing_predict_url_fails_every_site(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sans PREDICT_URL, le job échoue en le disant plutôt qu'en plantant."""
    monkeypatch.setenv("PREDICT_URL", "")
    get_settings.cache_clear()

    with caplog.at_level("ERROR", logger="app.jobs.predict_refresh"):
        code = await predict_refresh.refresh_all()

    assert code == 1
    assert serving.called_sites == []
    assert "PREDICT_URL" in caplog.text


async def test_empty_referential_returns_non_zero(
    serving: ServingDouble,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Aucun site actif : ce n'est pas un succès, c'est une base suspecte."""

    async def no_sites(session: AsyncSession) -> list[str]:
        return []

    monkeypatch.setattr(predict_refresh, "active_site_ids", no_sites)

    with caplog.at_level("ERROR", logger="app.jobs.predict_refresh"):
        code = await predict_refresh.refresh_all()

    assert code == 1
    assert "Aucun site actif" in caplog.text


def serving_answering_version(
    serving: ServingDouble,
    monkeypatch: pytest.MonkeyPatch,
    model_version: str,
) -> None:
    """Fait répondre le double avec la version de modèle donnée."""

    def build_client() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            site_id = json.loads(request.content)["site_id"]
            serving.called_sites.append(site_id)
            return httpx.Response(
                200,
                json={**prediction_for(site_id), "model_version": model_version},
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.predict_client.build_client", build_client)


async def test_model_absent_from_the_registry_fails_the_site(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Version inconnue du registre : le site échoue, rien n'est archivé.

    C'est le déploiement dégradé où le service charge un modèle par chemin
    d'artefact et retombe sur son identifiant interne. prediction.modele_id
    étant NOT NULL, il n'y a rien à quoi rattacher la ligne.
    """
    serving_answering_version(serving, monkeypatch, UNKNOWN_MODEL_VERSION)

    with caplog.at_level("ERROR", logger="app.jobs.predict_refresh"):
        code = await predict_refresh.refresh_all()

    assert code == 1
    assert await count_predictions(clean_predictions) == 0
    assert "aucun modele en base" in caplog.text


async def test_ambiguous_version_fails_the_site(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deux modèles portent la version et aucun n'est actif : refus explicite.

    La clé de `modele` est (nom, version) et le contrat ne transporte pas le
    nom. Choisir reviendrait à attribuer la prévision à un modèle qui ne l'a
    pas produite.
    """
    serving_answering_version(serving, monkeypatch, AMBIGUOUS_MODEL_VERSION)

    with caplog.at_level("ERROR", logger="app.jobs.predict_refresh"):
        code = await predict_refresh.refresh_all()

    assert code == 1
    assert await count_predictions(clean_predictions) == 0
    assert "modeles portent la version" in caplog.text


async def test_active_version_arbitrates_a_shared_version(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    """Version partagée mais une seule active : c'est elle qui rattache.

    `actif` désigne le modèle en service, et la promotion garantit qu'il n'y
    en a qu'un par nom.
    """
    from tests.conftest import (
        ARBITRATED_MODEL_NAME,
        ARBITRATED_MODEL_VERSION,
        modele_id_of,
    )

    expected = await modele_id_of(
        db_session,
        ARBITRATED_MODEL_VERSION,
        ARBITRATED_MODEL_NAME,
    )
    serving_answering_version(serving, monkeypatch, ARBITRATED_MODEL_VERSION)

    code = await predict_refresh.refresh_all()

    assert code == 0
    await clean_predictions.rollback()
    rattachements = set(
        await clean_predictions.scalars(select(Prediction.modele_id)),
    )
    assert rattachements == {expected}


async def test_empty_series_counts_as_success(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serving répond sans point : le site est traité, zéro ligne archivée.

    Ce n'est pas une panne : Serving a répondu, il n'avait simplement rien à
    prévoir.
    """

    def build_client() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            site_id = json.loads(request.content)["site_id"]
            return httpx.Response(
                200,
                json={**prediction_for(site_id), "points": []},
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.predict_client.build_client", build_client)

    code = await predict_refresh.refresh_all()

    assert code == 0
    assert await count_predictions(clean_predictions) == 0


async def test_main_returns_the_exit_code_and_releases_the_engine(
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le point d'entrée rend le code de sortie et ferme le pool.

    Le moteur est substitué : celui de l'application viserait DATABASE_URL, et
    le job de test n'a rien à y faire.
    """
    disposed: list[bool] = []

    class EngineDouble:
        async def dispose(self) -> None:
            disposed.append(True)

    monkeypatch.setattr(predict_refresh, "get_engine", EngineDouble)

    code = await predict_refresh.main()

    assert code == 0
    assert disposed == [True]


class TestRegistreVide:
    """Un registre vide n'est pas un service en panne.

    Les deux sortaient jusqu'ici sous la même exception, et l'astreinte
    partait donc chercher une panne d'infrastructure là où il n'y avait
    simplement rien à servir — l'état normal du projet tant qu'aucun modèle
    n'est promu.

    La distinction porte sur le CODE DE STATUT et jamais sur le texte du
    message : c'est le contrat de predict qui déclare le 503, un message est
    libre de changer sans PR de contrat.
    """

    async def test_un_503_sort_en_serving_not_ready(
        self,
        serving: ServingDouble,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def build_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        503,
                        json={"detail": "Aucun modele resolu par le registre."},
                    ),
                ),
            )

        monkeypatch.setattr("app.predict_client.build_client", build_client)
        with pytest.raises(ServingNotReadyError):
            await request_prediction(SITE_WITH_READINGS, 24)

    async def test_une_autre_erreur_reste_indifferenciee(
        self,
        serving: ServingDouble,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Un 500 est un incident : il ne doit pas emprunter le chemin du mode
        # dégradé, qui existe pour un registre vide.
        def build_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(500, text="boom"),
                ),
            )

        monkeypatch.setattr("app.predict_client.build_client", build_client)
        with pytest.raises(ServingUnavailableError) as raised:
            await request_prediction(SITE_WITH_READINGS, 24)
        assert not isinstance(raised.value, ServingNotReadyError)

    async def test_le_mode_degrade_est_rattrape_comme_une_panne(self) -> None:
        # Sous-classe, donc tout appelant qui ne veut pas distinguer les deux
        # continue de fonctionner sans changement. C'est ce qui permet
        # d'ajouter la distinction sans toucher au job.
        assert issubclass(ServingNotReadyError, ServingUnavailableError)

    async def test_un_registre_vide_narchive_rien(
        self,
        serving: ServingDouble,
        clean_predictions: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def build_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(503, json={"detail": "vide"}),
                ),
            )

        monkeypatch.setattr("app.predict_client.build_client", build_client)
        code = await predict_refresh.refresh_all()
        # La tournée échoue quand même : rien n'a été archivé, et le dire est
        # le rôle du code de sortie. C'est le NIVEAU de journal qui change.
        assert code == 1
        assert await count_predictions(clean_predictions) == 0


# --- Clé de service présentée au service d'inférence -------------------------
#
# Le service d'inférence relaie POST /api/v1/simulate/spike, qui écrit sur la
# source : il exige une clé sur les routes du contrat. Sans elle, tous les
# appels de l'API reviennent en 401 et le dashboard perd ses prédictions.


def test_la_cle_de_service_est_presentee_quand_elle_est_configuree() -> None:
    from app.predict_client import API_KEY_HEADER, service_headers

    assert service_headers("une-cle") == {API_KEY_HEADER: "une-cle"}


def test_aucune_cle_ne_produit_aucun_en_tete() -> None:
    """Envoyer une chaîne vide rendrait le journal du service illisible.

    Il distingue « en-tête absent » de « clé fausse » ; une clé vide se
    présenterait comme une clé fausse alors que c'est le mode ouvert du poste
    de développement.
    """
    from app.predict_client import service_headers

    assert service_headers("") == {}


# --- Base indisponible au démarrage ------------------------------------------
#
# Au redémarrage de l'hôte, le démon Docker relance tous les conteneurs
# ensemble sans lire les `depends_on` du compose : le job gagne la course sur
# TimescaleDB le temps qu'il rejoue son WAL. L'exception traversait alors la
# pile jusqu'à `SystemExit`, et le journal montrait quarante lignes de trace
# asyncpg pour ce qui n'est qu'un ordre de démarrage.


class EngineDouble:
    """Moteur factice : `main` ferme le pool dans son `finally`."""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


@pytest.fixture
def failing_round(monkeypatch: pytest.MonkeyPatch) -> Callable[[Exception], None]:
    """Fait échouer la tournée sur l'erreur donnée, sans toucher au vrai pool."""
    engine = EngineDouble()
    monkeypatch.setattr(predict_refresh, "get_engine", lambda: engine)

    def fail_with(error: Exception) -> None:
        async def refuse() -> int:
            raise error

        monkeypatch.setattr(predict_refresh, "refresh_all", refuse)

    return fail_with


async def test_une_base_qui_demarre_se_dit_en_une_ligne_d_information(
    failing_round: Callable[[Exception], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C'est un ordre de démarrage, pas un incident : INFO, et pas de trace."""
    failing_round(
        asyncpg.exceptions.CannotConnectNowError(
            "the database system is starting up",
        ),
    )

    with caplog.at_level("INFO", logger="app.jobs.predict_refresh"):
        code = await predict_refresh.main()

    assert code == 1
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "INFO"
    assert caplog.records[0].exc_info is None
    assert "base en attente" in caplog.text


async def test_une_base_injoignable_reste_une_erreur(
    failing_round: Callable[[Exception], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Injoignable n'est pas « en train de démarrer » : ça reste une erreur.

    Une base absente pendant une heure doit se voir dans le journal comme
    telle, sinon la boucle horaire tournerait à vide en silence.
    """
    failing_round(ConnectionRefusedError("Connect call failed ('172.24.0.4', 5432)"))

    with caplog.at_level("INFO", logger="app.jobs.predict_refresh"):
        code = await predict_refresh.main()

    assert code == 1
    assert caplog.records[0].levelname == "ERROR"
    assert "base injoignable" in caplog.text


async def test_le_moteur_est_ferme_meme_quand_la_base_manque(
    failing_round: Callable[[Exception], None],
) -> None:
    """Le `finally` reste la seule chose qui ferme le pool."""
    failing_round(ConnectionRefusedError("connexion refusée"))

    await predict_refresh.main()

    assert predict_refresh.get_engine().disposed


async def test_une_erreur_de_code_n_est_pas_maquillee_en_base_absente(
    failing_round: Callable[[Exception], None],
) -> None:
    """Le filet ne couvre que la connexion : le reste doit remonter.

    Sans cette limite, une faute de frappe dans la tournée se lirait « base
    injoignable » et on chercherait la panne du mauvais côté.
    """
    failing_round(AttributeError("'NoneType' object has no attribute 'site_id'"))

    with pytest.raises(AttributeError):
        await predict_refresh.main()


def test_les_etats_de_demarrage_de_postgres_sont_reconnus() -> None:
    starting = asyncpg.exceptions.CannotConnectNowError(
        "the database system is starting up",
    )

    assert predict_refresh.is_db_warming_up(starting)
    assert not predict_refresh.is_db_warming_up(ConnectionRefusedError("refusé"))


def test_la_raison_du_driver_tient_sur_une_ligne() -> None:
    """SQLAlchemy ajoute un lien vers sa documentation sous le message.

    Le journal d'une boucle horaire n'a besoin que de la première ligne.
    """
    origin = Exception("connection failed: FATAL: starting up\nseconde ligne")
    wrapped = OperationalError("SELECT 1", {}, origin)

    assert predict_refresh.db_error_line(wrapped) == (
        "connection failed: FATAL: starting up"
    )


def test_une_erreur_sans_message_se_dit_par_son_type() -> None:
    assert predict_refresh.db_error_line(ConnectionRefusedError()) == (
        "ConnectionRefusedError"
    )
