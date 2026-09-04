"""Les deux routes de la source qui n'atterrissaient nulle part, et la mise à
l'écart qui n'était publiée nulle part.

Trois manques distincts, corrigés ensemble parce qu'ils ont la même cause :
une donnée existait — ou aurait pu exister — sans que le contrat la serve.

- `GET /alerts` répondait 501 depuis l'origine. Le DTO était pourtant gelé
  avec ses huit champs : il ne manquait que la collecte et la table ;
- l'état des capteurs n'existait ni en base ni au contrat. `null_reasons` dit
  ce qui manquait à une mesure, jamais quel capteur est en cause ni jusqu'à
  quand la source annonce qu'il le restera ;
- `mesure_exclu` était peuplée par l'ETL depuis EV-08 et aucune route ne la
  lisait. La donnée était là, invisible.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import (
    ALERT_CRITICAL,
    ALERT_LOW,
    ALERT_OTHER_SITE,
    EXCLUDED_READING_INDEX,
    EXCLUSION_REASON,
    MINUTE,
    PANNE_CLOSE_CAPTEUR,
    PANNE_EN_COURS_CAPTEUR,
    SITE_WITH_READINGS,
    T0,
)

pytestmark = pytest.mark.anyio

FULL_WINDOW = {
    "start_time": T0.isoformat(),
    "end_time": (T0 + 4 * MINUTE).isoformat(),
}


class TestAlertes:
    async def test_les_alertes_sortent_de_la_plus_recente_a_la_plus_ancienne(
        self,
        api_client: AsyncClient,
    ) -> None:
        # On consulte les alertes pour savoir ce qui vient de se produire,
        # jamais pour remonter à l'origine des temps.
        response = await api_client.get("/api/v1/alerts")

        assert response.status_code == 200
        ids = [alert["alert_id"] for alert in response.json()]
        assert ids == [ALERT_OTHER_SITE, ALERT_CRITICAL, ALERT_LOW]

    async def test_les_champs_de_la_source_sont_servis_sous_leurs_noms(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(
            "/api/v1/alerts", params={"site_id": SITE_WITH_READINGS}
        )

        alert = next(
            item for item in response.json() if item["alert_id"] == ALERT_CRITICAL
        )
        assert alert["site_id"] == SITE_WITH_READINGS
        assert alert["severity"] == "critical"
        # La colonne s'appelle type_alerte, `type` étant trop générique pour
        # une colonne. Le contrat publie le nom de la source.
        assert alert["type"] == "outage"
        assert alert["value"] == 812.5
        assert alert["threshold"] == 720.0
        # `timestamp` date le déclenchement côté source, pas la collecte.
        assert alert["timestamp"].startswith("2026-09-01T12:01")

    async def test_une_alerte_de_capteur_est_servie_sans_valeur_ni_seuil(
        self,
        api_client: AsyncClient,
    ) -> None:
        # C'est le cas qui justifie que les deux champs soient nullables :
        # perdre une alerte parce qu'il lui manque un chiffre serait pire que
        # la servir sans.
        response = await api_client.get(
            "/api/v1/alerts", params={"severity": "low"}
        )

        alerts = response.json()
        assert [alert["alert_id"] for alert in alerts] == [ALERT_LOW]
        assert alerts[0]["value"] is None
        assert alerts[0]["threshold"] is None

    async def test_les_filtres_se_combinent(self, api_client: AsyncClient) -> None:
        response = await api_client.get(
            "/api/v1/alerts",
            params={"site_id": SITE_WITH_READINGS, "type": "outage"},
        )

        assert [alert["alert_id"] for alert in response.json()] == [ALERT_CRITICAL]

    async def test_un_site_inconnu_rend_une_liste_vide_et_non_404(
        self,
        api_client: AsyncClient,
    ) -> None:
        # Ici le site est un filtre, pas une ressource : une liste vide répond
        # exactement à la question posée.
        response = await api_client.get(
            "/api/v1/alerts", params={"site_id": "SITE999"}
        )

        assert response.status_code == 200
        assert response.json() == []

    async def test_une_gravite_inconnue_est_refusee(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(
            "/api/v1/alerts", params={"severity": "catastrophique"}
        )

        assert response.status_code == 422

    async def test_les_alertes_exigent_un_jeton(
        self,
        anonymous_client: AsyncClient,
    ) -> None:
        response = await anonymous_client.get("/api/v1/alerts")

        assert response.status_code == 401


class TestEtatDesCapteurs:
    async def test_les_cinq_capteurs_sont_servis_tries(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(
            f"/api/v1/sites/{SITE_WITH_READINGS}/sensors"
        )

        assert response.status_code == 200
        assert [row["capteur"] for row in response.json()] == [
            "consumption",
            "electrical",
            "humidity",
            "network",
            "temperature",
        ]

    async def test_la_date_de_retablissement_annoncee_est_servie(
        self,
        api_client: AsyncClient,
    ) -> None:
        # C'est la seule information qu'aucune mesure ne porte : `null_reasons`
        # dit ce qui a manqué, jamais jusqu'à quand cela manquera.
        response = await api_client.get(
            f"/api/v1/sites/{SITE_WITH_READINGS}/sensors"
        )

        rows = {row["capteur"]: row for row in response.json()}
        assert rows["temperature"]["statut"] == "failing"
        assert rows["temperature"]["failing_until"].startswith("2026-09-01T12:05")
        assert rows["network"]["statut"] == "ok"
        assert rows["network"]["failing_until"] is None
        # La synthèse du site est recopiée sur chaque capteur : la table est
        # plate, et la connaître ne doit pas demander une agrégation.
        assert {row["overall"] for row in response.json()} == {"degraded"}

    async def test_un_site_sans_releve_rend_une_liste_vide(
        self,
        api_client: AsyncClient,
    ) -> None:
        # Distinct du 404 : le site existe, c'est la collecte qui n'a rien
        # relevé. Les deux appellent des conduites opposées.
        response = await api_client.get("/api/v1/sites/SITE003/sensors")

        assert response.status_code == 200
        assert response.json() == []

    async def test_un_site_inconnu_donne_404(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/api/v1/sites/SITE999/sensors")

        assert response.status_code == 404

    async def test_les_capteurs_exigent_un_jeton(
        self,
        anonymous_client: AsyncClient,
    ) -> None:
        response = await anonymous_client.get(
            f"/api/v1/sites/{SITE_WITH_READINGS}/sensors"
        )

        assert response.status_code == 401


class TestMiseALEcart:
    async def test_une_mesure_ecartee_le_dit_avec_son_motif(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(
            f"/api/v1/sites/{SITE_WITH_READINGS}/readings", params=FULL_WINDOW
        )

        items = response.json()["items"]
        excluded = [item for item in items if item["excluded"]]
        assert len(excluded) == 1
        assert excluded[0]["exclusion_reason"] == EXCLUSION_REASON
        assert excluded[0]["timestamp"].startswith("2026-09-01T12:02")

    async def test_une_mesure_ecartee_reste_servie(
        self,
        api_client: AsyncClient,
    ) -> None:
        """La jointure est externe, et c'est tout ce qui compte ici.

        Une jointure interne ne rendrait que les mesures écartées, soit
        l'inverse de ce que le contrat publie. Et l'API ne cache pas une
        mesure écartée : c'est le consommateur qui la retire de ses moyennes.
        """
        response = await api_client.get(
            f"/api/v1/sites/{SITE_WITH_READINGS}/readings", params=FULL_WINDOW
        )

        items = response.json()["items"]
        assert len(items) == 5
        assert sum(1 for item in items if not item["excluded"]) == 4

    async def test_une_mesure_non_ecartee_n_a_pas_de_motif(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(
            f"/api/v1/sites/{SITE_WITH_READINGS}/readings", params=FULL_WINDOW
        )

        kept = [item for item in response.json()["items"] if not item["excluded"]]
        assert all(item["exclusion_reason"] is None for item in kept)

    async def test_la_derniere_mesure_porte_aussi_la_mise_a_l_ecart(
        self,
        api_client: AsyncClient,
    ) -> None:
        # Les deux routes de lecture doivent dire la même chose d'une même
        # mesure : une seule des deux jointures aurait suffi à les désaccorder.
        response = await api_client.get(
            f"/api/v1/sites/{SITE_WITH_READINGS}/readings/latest"
        )

        body = response.json()
        assert "excluded" in body
        assert "exclusion_reason" in body
        # La plus récente n'est pas celle qui est écartée.
        assert body["excluded"] is False
        assert EXCLUDED_READING_INDEX != 4


class TestHistoriqueDesPannes:
    """Ce que /sensors ne peut pas dire, en ne gardant que le présent.

    Un capteur tombé puis rétabli depuis n'apparaît que dans cette route :
    `capteur_etat` l'a déjà reposé à `ok`, et `mesure.null_reasons` ne connaît
    que les pannes visibles SUR une mesure.
    """

    URL = f"/api/v1/sites/{SITE_WITH_READINGS}/sensors/history"

    async def test_les_episodes_sortent_du_plus_recent_au_plus_ancien(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(self.URL)

        assert response.status_code == 200
        assert [row["capteur"] for row in response.json()] == [
            PANNE_EN_COURS_CAPTEUR,
            PANNE_CLOSE_CAPTEUR,
        ]

    async def test_une_panne_close_porte_ses_deux_bornes(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(self.URL)
        close = next(
            row for row in response.json() if row["capteur"] == PANNE_CLOSE_CAPTEUR
        )

        assert close["ongoing"] is False
        assert close["ended_at"] is not None
        assert close["started_at"] < close["ended_at"]

    async def test_une_panne_en_cours_n_a_pas_de_fin(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(self.URL)
        ouverte = next(
            row for row in response.json() if row["capteur"] == PANNE_EN_COURS_CAPTEUR
        )

        assert ouverte["ongoing"] is True
        assert ouverte["ended_at"] is None
        # La date de rétablissement annoncée reste servie : c'est une prévision
        # de la source, pas un constat, et elle peut être dépassée.
        assert ouverte["failing_until"] is not None

    async def test_le_filtre_en_cours_isole_les_pannes_ouvertes(
        self,
        api_client: AsyncClient,
    ) -> None:
        ouvertes = await api_client.get(self.URL, params={"ongoing": True})
        closes = await api_client.get(self.URL, params={"ongoing": False})

        assert [row["capteur"] for row in ouvertes.json()] == [
            PANNE_EN_COURS_CAPTEUR
        ]
        assert [row["capteur"] for row in closes.json()] == [PANNE_CLOSE_CAPTEUR]

    async def test_le_filtre_par_capteur_est_pris_en_compte(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(
            self.URL, params={"capteur": PANNE_CLOSE_CAPTEUR}
        )

        assert [row["capteur"] for row in response.json()] == [PANNE_CLOSE_CAPTEUR]

    async def test_un_site_inconnu_donne_404(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/api/v1/sites/SITE999/sensors/history")

        assert response.status_code == 404

    async def test_l_historique_exige_un_jeton(
        self,
        anonymous_client: AsyncClient,
    ) -> None:
        response = await anonymous_client.get(self.URL)

        assert response.status_code == 401
