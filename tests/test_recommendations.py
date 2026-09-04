"""Les recommandations d'action (EV-32), et ce qui les fonde.

Trois règles, trois interlocuteurs, et aucune hiérarchie entre elles : un site
peut recevoir les trois en même temps. Ce qui est éprouvé ici n'est pas la
formulation — elle changera — mais les décisions qu'elle porte :

- une pointe est une PLAGE contiguë, pas l'ensemble des points hauts. Dire
  « décaler hors de 08h–20h » parce que deux sommets distants dépassent ne
  serait pas un conseil ;
- un dépassement de capacité est absolu, là où la pointe est relative. Un site
  à 40 % de sa puissance souscrite a lui aussi des heures creuses, mais il n'a
  rien à délester ;
- une liste vide a deux causes opposées — « rien à signaler » et « aucune
  prévision » — et les confondre ferait passer une panne pour une accalmie.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.energy import Site
from app.services.recommendations import (
    NO_PREDICTION,
    NOTHING_TO_REPORT,
    recommendations_for,
)
from tests.conftest import (
    RECO_OVERRUN_KW,
    RECO_PEAK_KW,
    RECO_SITE,
    SITE_WITHOUT_READINGS,
)

pytestmark = pytest.mark.anyio

RECOMMENDATIONS_URL = f"/api/v1/sites/{RECO_SITE}/recommendations"


def by_type(body: dict) -> dict[str, dict]:
    """Indexe les actions servies par leur nature."""
    return {item["type"]: item for item in body["items"]}


class TestParLaRoute:
    async def test_les_trois_regles_peuvent_tomber_ensemble(
        self,
        api_client: AsyncClient,
        recommendation_site: datetime,
    ) -> None:
        response = await api_client.get(RECOMMENDATIONS_URL)

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(by_type(body)) == {
            "predicted_peak",
            "capacity_overrun",
            "sensor_failure",
        }
        assert body["detail"] is None

    async def test_les_actions_sortent_de_la_plus_urgente_a_la_moins(
        self,
        api_client: AsyncClient,
        recommendation_site: datetime,
    ) -> None:
        # Le tri porte sur la gravité et non sur la nature : les trois règles
        # s'adressent à trois interlocuteurs, aucune ne prime par principe.
        response = await api_client.get(RECOMMENDATIONS_URL)

        severities = [item["severity"] for item in response.json()["items"]]
        assert severities == ["critical", "high", "medium"]

    async def test_le_delestage_nomme_la_puissance_et_l_heure(
        self,
        api_client: AsyncClient,
        recommendation_site: datetime,
    ) -> None:
        response = await api_client.get(RECOMMENDATIONS_URL)
        overrun = by_type(response.json())["capacity_overrun"]

        assert overrun["value_kw"] == RECO_OVERRUN_KW
        assert overrun["message"].startswith(f"Délester {RECO_OVERRUN_KW:.0f} kW à ")
        assert overrun["message"].endswith(
            ", ou réviser la puissance souscrite"
        )
        # L'heure est aussi servie en champ structuré : un dashboard qui
        # devrait l'extraire d'une phrase française serait cassé par la
        # première reformulation.
        assert overrun["at"] is not None

    async def test_la_pointe_nomme_une_plage_et_non_un_instant(
        self,
        api_client: AsyncClient,
        recommendation_site: datetime,
    ) -> None:
        response = await api_client.get(RECOMMENDATIONS_URL)
        peak = by_type(response.json())["predicted_peak"]

        assert peak["window_start"] is not None
        assert peak["window_end"] is not None
        assert peak["window_start"] < peak["window_end"]
        assert peak["value_kw"] == RECO_PEAK_KW
        assert peak["message"].startswith(
            "Décaler les usages non critiques hors de la fenêtre "
        )

    async def test_la_panne_capteur_ne_nomme_ni_heure_ni_puissance(
        self,
        api_client: AsyncClient,
        recommendation_site: datetime,
    ) -> None:
        # Elle ne porte pas sur un instant de la prévision : elle dit que la
        # prévision elle-même est mal fondée.
        response = await api_client.get(RECOMMENDATIONS_URL)
        sensor = by_type(response.json())["sensor_failure"]

        assert sensor["at"] is None
        assert sensor["window_start"] is None
        assert sensor["value_kw"] is None
        assert sensor["message"] == (
            "Intervention capteur : la prévision s'appuie sur des données"
            " imputées, fiabilité dégradée"
        )

    async def test_la_version_du_modele_accompagne_les_conseils(
        self,
        api_client: AsyncClient,
        recommendation_site: datetime,
    ) -> None:
        # Un conseil ne vaut que ce que vaut le modèle qui le fonde.
        response = await api_client.get(RECOMMENDATIONS_URL)

        assert response.json()["model_version"] is not None

    async def test_sans_prevision_la_raison_est_dite(
        self,
        api_client: AsyncClient,
    ) -> None:
        """« Rien à signaler » et « aucune prévision » ne se confondent pas.

        Le premier rassure, le second doit inquiéter. Une liste vide sans
        raison les rendrait indiscernables.
        """
        response = await api_client.get(
            f"/api/v1/sites/{SITE_WITHOUT_READINGS}/recommendations"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["detail"] == NO_PREDICTION
        assert body["model_version"] is None

    async def test_un_site_inconnu_donne_404(self, api_client: AsyncClient) -> None:
        response = await api_client.get(
            "/api/v1/sites/SITE999/recommendations"
        )

        assert response.status_code == 404

    async def test_un_horizon_hors_bornes_est_refuse(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(
            RECOMMENDATIONS_URL, params={"horizon_hours": 0}
        )

        assert response.status_code == 422

    async def test_les_recommandations_exigent_un_jeton(
        self,
        anonymous_client: AsyncClient,
    ) -> None:
        response = await anonymous_client.get(RECOMMENDATIONS_URL)

        assert response.status_code == 401


class TestLesRegles:
    """Le moteur seul, avec un présent injecté.

    Passer par la route imposerait de semer des prévisions relatives à
    l'horloge à chaque cas ; ici le présent est un paramètre, et chaque règle
    s'éprouve sur le jeu qu'elle demande.
    """

    async def test_un_site_sous_sa_capacite_n_a_rien_a_delester(
        self,
        db_session: AsyncSession,
        recommendation_site: datetime,
    ) -> None:
        # La capacité est relevée au-dessus du sommet prévu : la pointe reste,
        # le délestage disparaît. C'est ce qui distingue une règle absolue
        # d'une règle relative.
        site = await db_session.get(Site, RECO_SITE)
        site.capacity_kw = 10_000
        await db_session.flush()

        result = await recommendations_for(db_session, site)

        assert "capacity_overrun" not in {item.type for item in result.items}
        assert "predicted_peak" in {item.type for item in result.items}

    async def test_une_prevision_plate_ne_dessine_aucune_pointe(
        self,
        db_session: AsyncSession,
        recommendation_site: datetime,
    ) -> None:
        """Tous les points au-dessus du seuil relatif : la plage couvre tout.

        Une « pointe » qui court sur toute la fenêtre ne dit plus rien, mais
        elle reste une plage contiguë : c'est le dépassement de capacité qui
        doit alors porter l'alerte, pas elle.
        """
        site = await db_session.get(Site, RECO_SITE)
        result = await recommendations_for(db_session, site, horizon_hours=1)

        # Une seule heure examinée : moins que le minimum de points d'une
        # plage, donc aucune pointe.
        assert "predicted_peak" not in {item.type for item in result.items}

    async def test_un_site_sain_et_sans_depassement_le_dit(
        self,
        db_session: AsyncSession,
        recommendation_site: datetime,
    ) -> None:
        site = await db_session.get(Site, RECO_SITE)
        site.capacity_kw = 10_000
        await db_session.flush()
        # Un présent postérieur à toute la fenêtre : plus aucune prévision à
        # venir, mais la panne capteur reste, elle ne dépend pas d'elles.
        result = await recommendations_for(
            db_session,
            site,
            now=datetime.now(UTC) + timedelta(days=30),
        )

        assert result.detail == NO_PREDICTION
        assert [item.type for item in result.items] == ["sensor_failure"]

    async def test_sans_panne_ni_depassement_la_liste_vide_est_rassurante(
        self,
        db_session: AsyncSession,
        recommendation_site: datetime,
    ) -> None:
        # Capacité hors d'atteinte, capteurs retirés, horizon d'une heure :
        # il reste des prévisions, et rien à en dire.
        from sqlalchemy import delete, update

        from app.models.energy import Mesure
        from app.models.observation import CapteurEtat

        site = await db_session.get(Site, RECO_SITE)
        site.capacity_kw = 10_000
        await db_session.execute(
            delete(CapteurEtat).where(CapteurEtat.site_id == RECO_SITE)
        )
        await db_session.execute(
            update(Mesure)
            .where(Mesure.site_id == RECO_SITE)
            .values(imputation_method="none")
        )
        await db_session.flush()

        result = await recommendations_for(db_session, site, horizon_hours=1)

        assert result.items == []
        assert result.detail == NOTHING_TO_REPORT
