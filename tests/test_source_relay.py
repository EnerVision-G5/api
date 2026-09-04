"""Les trois endpoints qui relaient la source : pic, historique, référentiel.

L'API ne connaît pas l'API Mock. Elle passe par le service d'inférence, seul
côté predict à lui parler, et c'est ce qui explique la forme de ces tests :
Serving est un httpx.MockTransport, la source n'existe pas, et ce qui est
vérifié est ce que l'API en fait — ce qu'elle écrit en base et ce qu'elle
rend au dashboard.

Deux comportements comptent plus que les autres, parce qu'ils portent une
décision et non une mécanique :

- un pic déclenché puis mal relu reste un succès archivé. Le pic a eu lieu ;
  répondre en erreur inviterait à rejouer l'appel, donc à superposer deux
  pics sur le même site ;
- une synchronisation ne remplace jamais le référentiel, elle le repose. Une
  source momentanément incomplète effacerait sinon des sites, et avec eux
  toutes les mesures qui les référencent.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.energy import Site
from app.models.simulation import SimulationPic
from tests.conftest import WRITER_USERNAME

pytestmark = pytest.mark.anyio

SERVING_BASE_URL = "http://serving.invalid"
SITE = "SITE001"

SPIKE_BODY = {
    "site_id": SITE,
    "status": "simulated",
    "event": "consumption_spike",
    "duration_minutes": 60,
    "message": "Pic de consommation simulé sur Bureau Paris La Défense.",
    "simulated_at": "2026-09-04T14:32:00Z",
    "reading": {"consumption_kw": 812.5, "data_quality": "good"},
}

SITES_BODY = [
    {
        "site_id": SITE,
        "site_type": "office",
        "site_name": "Bureau Paris La Défense — renommé",
        "location": "Paris, France",
        "capacity_kw": 250,
        "status": "active",
    },
    {
        # Sans capacity_kw : la base la refuserait, le site est écarté.
        "site_id": "SITE004",
        "site_type": "unknown",
        "site_name": "Site 4",
        "location": None,
        "capacity_kw": None,
        "status": "active",
    },
]


class ServingDouble:
    """Serving réduit à ce que l'API lui demande, sans réseau.

    `requests` sert autant que les réponses : plusieurs tests vérifient qu'un
    appel n'est PAS parti — rôle insuffisant, durée hors bornes, site inconnu.
    Un pic évité ne laisse aucune trace ailleurs.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: dict[str, httpx.Response] = {}

    def answer(self, prefix: str, response: httpx.Response) -> None:
        self.responses[prefix] = response

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for prefix, response in self.responses.items():
            if request.url.path.startswith(prefix):
                return response
        return httpx.Response(404, json={"detail": "route inconnue"})


@pytest.fixture
def serving(monkeypatch: pytest.MonkeyPatch) -> Iterator[ServingDouble]:
    """Branche l'API sur un Serving factice."""
    double = ServingDouble()

    def build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(double.handle))

    monkeypatch.setattr("app.predict_client.build_client", build_client)
    monkeypatch.setenv("PREDICT_URL", SERVING_BASE_URL)
    get_settings.cache_clear()
    yield double
    monkeypatch.delenv("PREDICT_URL", raising=False)
    get_settings.cache_clear()


class TestDeclencherUnPic:
    async def test_le_pic_est_archive_avec_ce_que_la_source_a_servi(
        self,
        writer_client: AsyncClient,
        db_session: AsyncSession,
        serving: ServingDouble,
    ) -> None:
        serving.answer("/api/v1/simulate/spike/", httpx.Response(200, json=SPIKE_BODY))

        response = await writer_client.post(
            f"/api/v1/simulations/spike/{SITE}",
            params={"duration_minutes": 60},
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["site_id"] == SITE
        assert body["duration_minutes"] == 60
        assert body["statut"] == "simulated"
        assert body["consumption_kw_constatee"] == 812.5
        assert body["declenche_par"] == WRITER_USERNAME

        # La trace existe en base : c'est elle qui distinguera plus tard une
        # pointe provoquée d'une vraie dans `mesure`.
        row = await db_session.scalar(
            select(SimulationPic).where(
                SimulationPic.simulation_id == body["simulation_id"]
            )
        )
        assert row is not None
        assert row.evenement == "consumption_spike"
        assert row.data_quality_constatee == "good"

    async def test_un_pic_mal_relu_reste_un_succes_archive(
        self,
        writer_client: AsyncClient,
        db_session: AsyncSession,
        serving: ServingDouble,
    ) -> None:
        # Serving a déclenché le pic et le dit ; il n'a simplement pas pu
        # relire la mesure. Rendre 502 ici ferait rejouer l'appel.
        body = dict(SPIKE_BODY, reading=None)
        serving.answer("/api/v1/simulate/spike/", httpx.Response(200, json=body))

        response = await writer_client.post(f"/api/v1/simulations/spike/{SITE}")

        assert response.status_code == 201
        assert response.json()["consumption_kw_constatee"] is None
        row = await db_session.scalar(
            select(SimulationPic).where(
                SimulationPic.simulation_id == response.json()["simulation_id"]
            )
        )
        assert row is not None
        assert row.data_quality_constatee is None

    async def test_un_site_inconnu_est_refuse_avant_le_declenchement(
        self,
        writer_client: AsyncClient,
        serving: ServingDouble,
    ) -> None:
        # La clé étrangère refuserait la ligne de toute façon, mais après
        # avoir provoqué un pic bien réel sur la source.
        response = await writer_client.post("/api/v1/simulations/spike/SITE999")

        assert response.status_code == 404
        assert serving.requests == []

    async def test_une_source_muette_ne_laisse_aucune_trace(
        self,
        writer_client: AsyncClient,
        db_session: AsyncSession,
        serving: ServingDouble,
    ) -> None:
        serving.answer(
            "/api/v1/simulate/spike/",
            httpx.Response(502, json={"detail": "source injoignable"}),
        )

        before = len((await db_session.scalars(select(SimulationPic))).all())
        response = await writer_client.post(f"/api/v1/simulations/spike/{SITE}")
        await db_session.commit()
        after = len((await db_session.scalars(select(SimulationPic))).all())

        assert response.status_code == 502
        # Aucun pic n'a eu lieu : en archiver un dirait le contraire.
        assert after == before

    async def test_un_reader_ne_peut_pas_declencher(
        self,
        api_client: AsyncClient,
        serving: ServingDouble,
    ) -> None:
        response = await api_client.post(f"/api/v1/simulations/spike/{SITE}")

        assert response.status_code == 403
        assert serving.requests == []

    async def test_une_duree_hors_bornes_est_refusee(
        self,
        writer_client: AsyncClient,
        serving: ServingDouble,
    ) -> None:
        response = await writer_client.post(
            f"/api/v1/simulations/spike/{SITE}",
            params={"duration_minutes": 241},
        )

        assert response.status_code == 422
        assert serving.requests == []


class TestHistoriqueDesPics:
    async def test_les_pics_sortent_du_plus_recent_au_plus_ancien(
        self,
        writer_client: AsyncClient,
        serving: ServingDouble,
    ) -> None:
        serving.answer("/api/v1/simulate/spike/", httpx.Response(200, json=SPIKE_BODY))
        first = await writer_client.post(f"/api/v1/simulations/spike/{SITE}")
        second = await writer_client.post(f"/api/v1/simulations/spike/{SITE}")

        response = await writer_client.get(
            "/api/v1/simulations/spike", params={"site_id": SITE}
        )

        assert response.status_code == 200
        ids = [row["simulation_id"] for row in response.json()]
        assert ids[:2] == [
            second.json()["simulation_id"],
            first.json()["simulation_id"],
        ]

    async def test_un_reader_peut_consulter_l_historique(
        self,
        api_client: AsyncClient,
    ) -> None:
        # La lecture n'est pas une écriture : le rôle writer n'a pas à être
        # exigé pour savoir ce qui s'est passé.
        response = await api_client.get("/api/v1/simulations/spike")

        assert response.status_code == 200

    async def test_l_historique_exige_un_jeton(
        self,
        anonymous_client: AsyncClient,
    ) -> None:
        response = await anonymous_client.get("/api/v1/simulations/spike")

        assert response.status_code == 401


class TestSynchroniserLeReferentiel:
    async def test_la_source_repose_le_referentiel_sans_le_remplacer(
        self,
        writer_client: AsyncClient,
        db_session: AsyncSession,
        serving: ServingDouble,
    ) -> None:
        serving.answer("/api/v1/sites", httpx.Response(200, json=SITES_BODY))
        # Le jeu de données est de portée session : ce test écrit vraiment
        # dans `site`, il doit donc rendre le référentiel tel qu'il l'a trouvé.
        original = await db_session.get(Site, SITE)
        before = (original.site_name, float(original.capacity_kw))

        try:
            response = await writer_client.post("/api/v1/sites/sync")

            assert response.status_code == 200, response.text
            body = response.json()
            # Deux sites servis, un seul écrit : celui sans capacité est
            # écarté, et l'écart entre les deux nombres est ce qui le signale.
            assert body["received"] == 2
            assert body["synchronized"] == 1

            # Les sites que la source n'a pas servis sont toujours là : une
            # source incomplète ne doit pas emporter les mesures qui les
            # référencent.
            known = {site["site_id"] for site in body["sites"]}
            assert {"SITE002", "SITE003"} <= known

            await db_session.rollback()
            renamed = await db_session.get(Site, SITE)
            await db_session.refresh(renamed)
            assert renamed.site_name == "Bureau Paris La Défense — renommé"
            assert float(renamed.capacity_kw) == 250.0
        finally:
            restored = await db_session.get(Site, SITE)
            restored.site_name, restored.capacity_kw = before
            await db_session.commit()

    async def test_une_source_muette_donne_502(
        self,
        writer_client: AsyncClient,
        serving: ServingDouble,
    ) -> None:
        serving.answer(
            "/api/v1/sites",
            httpx.Response(502, json={"detail": "source injoignable"}),
        )

        response = await writer_client.post("/api/v1/sites/sync")

        assert response.status_code == 502

    async def test_un_reader_ne_peut_pas_synchroniser(
        self,
        api_client: AsyncClient,
        serving: ServingDouble,
    ) -> None:
        response = await api_client.post("/api/v1/sites/sync")

        assert response.status_code == 403
        assert serving.requests == []
