"""Indicateurs de confiance dans la donnée (EV-18).

Trois indicateurs, et chacun existe parce qu'une question restait sans réponse.
Les tests sont rangés par question, pas par champ.

La fraîcheur d'ingestion : la donnée arrive-t-elle encore ? `mesure` ne peut
pas le dire seule, un collecteur arrêté n'y écrivant aucune ligne.

La part de mesures dégradées : que vaut ce qui arrive ? Et le piège qui va
avec — `data_quality` vaut `good` par défaut, donc une fenêtre non qualifiée
paraît saine.

L'écart prévision / réel : le modèle qui s'appuie dessus tient-il encore ?
Avec sa règle d'appariement, qui décide du chiffre autant que les données.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.indicators import is_drifting
from tests.conftest import (
    FRESH_BIAS_KW,
    FRESH_DEGRADED,
    FRESH_IMPUTED,
    FRESH_MAE_KW,
    FRESH_MEAN_ACTUAL_KW,
    FRESH_PAIRED_POINTS,
    FRESH_QUALIFIED,
    FRESH_SENSOR_FAILURES,
    FRESH_SITE,
    FRESH_TOTAL,
    NEVER_COLLECTED_FAILURES,
    NEVER_COLLECTED_SITE,
    SITE_UNKNOWN,
    SITE_WITH_READINGS,
    assert_error_response,
)

pytestmark = pytest.mark.anyio

COLLECTION_URL = "/api/v1/indicators"


def site_url(site_id: str) -> str:
    return f"/api/v1/sites/{site_id}/indicators"


class TestAcces:
    """Protégées comme les autres lectures, et pas plus."""

    async def test_la_collection_exige_un_jeton(
        self,
        anonymous_client: AsyncClient,
    ) -> None:
        response = await anonymous_client.get(COLLECTION_URL)
        assert response.status_code == 401
        assert_error_response(response.json())

    async def test_le_site_exige_un_jeton(
        self,
        anonymous_client: AsyncClient,
    ) -> None:
        response = await anonymous_client.get(site_url(SITE_WITH_READINGS))
        assert response.status_code == 401
        assert_error_response(response.json())

    async def test_un_site_inconnu_est_un_404(self, api_client: AsyncClient) -> None:
        # Et non des indicateurs à zéro : sur un site inconnu, ils se liraient
        # comme un site sain sans donnée, le contresens exact de ce que ces
        # trois chiffres servent à dire.
        response = await api_client.get(site_url(SITE_UNKNOWN))
        assert response.status_code == 404
        assert_error_response(response.json())

    async def test_une_fenetre_hors_bornes_est_refusee(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(
            site_url(SITE_WITH_READINGS),
            params={"window_hours": 0},
        )
        assert response.status_code == 422
        assert_error_response(response.json())


class TestRoutage:
    """La collection est au premier niveau, et ce n'est pas cosmétique."""

    async def test_la_collection_nest_pas_avalee_par_le_site(
        self,
        api_client: AsyncClient,
    ) -> None:
        # `/sites/indicators` serait résolu par `/sites/{site_id}`, déclaré
        # avant, et répondrait « Site inconnu : indicators ». C'est le piège
        # que la route de premier niveau évite, indépendamment de l'ordre des
        # include_router de main.py.
        avalee = await api_client.get("/api/v1/sites/indicators")
        assert avalee.status_code == 404

        premier_niveau = await api_client.get(COLLECTION_URL)
        assert premier_niveau.status_code == 200

    async def test_la_collection_couvre_le_referentiel(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Comparer sept sites ne doit pas coûter sept appels : c'est ce que
        # cette route existe pour éviter.
        body = (await api_client.get(COLLECTION_URL)).json()
        returned = [entry["site_id"] for entry in body]
        assert returned == sorted(returned)
        assert FRESH_SITE in returned
        assert NEVER_COLLECTED_SITE in returned

    async def test_les_deux_routes_disent_la_meme_chose(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Un seul chemin de calcul pour les deux routes : deux agrégats
        # séparés finiraient par diverger sans que rien ne le signale.
        collection = (await api_client.get(COLLECTION_URL)).json()
        seul = (await api_client.get(site_url(FRESH_SITE))).json()
        depuis_collection = next(
            entry for entry in collection if entry["site_id"] == FRESH_SITE
        )
        # Les bornes de fenêtre sont exclues de la comparaison : les deux
        # appels ont lieu à deux instants, et chacun mesure son propre
        # présent. Ce sont les comptes qui doivent coïncider.
        compared = ("total", "degraded", "qualified", "sensor_failure", "imputed")
        assert {key: seul["quality"][key] for key in compared} == {
            key: depuis_collection["quality"][key] for key in compared
        }
        assert seul["accuracy"]["paired_points"] == (
            depuis_collection["accuracy"]["paired_points"]
        )


class TestFraicheurDIngestion:
    """La donnée arrive-t-elle encore, et la chaîne suit-elle ?"""

    async def test_un_site_collecte_a_linstant_nest_pas_en_retard(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        body = (await api_client.get(site_url(FRESH_SITE))).json()
        ingestion = body["ingestion"]
        assert ingestion["is_stale"] is False
        assert ingestion["measure_age_seconds"] >= 0
        assert ingestion["last_measure_at"] is not None

    async def test_le_seuil_est_servi_avec_la_valeur(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Sans cela, le dashboard le redéclarerait, et deux vérités
        # divergeraient en silence — ce qui s'est déjà produit à 120 s côté
        # front contre 180 s côté collecteur.
        body = (await api_client.get(site_url(FRESH_SITE))).json()
        assert body["ingestion"]["stale_threshold_seconds"] == (
            get_settings().stale_threshold_seconds
        )

    async def test_un_site_dont_la_derniere_mesure_est_ancienne_est_en_retard(
        self,
        api_client: AsyncClient,
    ) -> None:
        # Le jeu de données figé du seed est daté de plusieurs jours : c'est
        # exactement ce qu'un site dont la collecte s'est arrêtée donnerait.
        body = (await api_client.get(site_url(SITE_WITH_READINGS))).json()
        ingestion = body["ingestion"]
        assert ingestion["is_stale"] is True
        assert ingestion["measure_age_seconds"] > (
            get_settings().stale_threshold_seconds
        )

    async def test_un_site_sans_aucune_mesure_est_en_retard(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Répondre « frais » ferait passer l'absence totale de donnée pour une
        # donnée à jour. Un âge inconnu n'est jamais tenu pour frais.
        body = (await api_client.get(site_url(NEVER_COLLECTED_SITE))).json()
        ingestion = body["ingestion"]
        assert ingestion["last_measure_at"] is None
        assert ingestion["measure_age_seconds"] is None
        assert ingestion["is_stale"] is True

    async def test_le_retard_decriture_est_distinct_de_lage_de_la_mesure(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Un collecteur arrêté et un collecteur qui rattrape du retard donnent
        # le même âge de mesure et deux retards d'écriture très différents.
        ingestion = (await api_client.get(site_url(FRESH_SITE))).json()["ingestion"]
        assert ingestion["ingestion_lag_seconds"] is not None
        assert ingestion["last_ingested_at"] is not None
        assert ingestion["ingestion_lag_seconds"] != (
            ingestion["measure_age_seconds"]
        )

    async def test_letat_du_collecteur_est_publie(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        collector = (await api_client.get(site_url(FRESH_SITE))).json()["ingestion"][
            "collector"
        ]
        assert collector["source"] == "poller"
        assert collector["consecutive_failures"] == 0
        assert collector["last_success_at"] is not None
        assert collector["last_data_lag_seconds"] == pytest.approx(12.5)

    async def test_un_collecteur_qui_tourne_et_echoue_est_visible(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # LA panne que `mesure` ne peut pas dire : aucune ligne écrite, donc
        # aucun agrégat ne bouge, et pourtant le collecteur crie.
        collector = (await api_client.get(site_url(NEVER_COLLECTED_SITE))).json()[
            "ingestion"
        ]["collector"]
        assert collector["last_success_at"] is None
        assert collector["last_attempt_at"] is not None
        assert collector["consecutive_failures"] == NEVER_COLLECTED_FAILURES
        assert collector["last_error"]

    async def test_un_site_sans_etat_de_collecte_le_dit(
        self,
        api_client: AsyncClient,
    ) -> None:
        # Nul et non un état à zéro : le collecteur n'a jamais tourné sur ce
        # site, ou la migration n'est pas appliquée. Les deux se disent « je
        # ne sais pas », jamais « tout va bien ».
        body = (await api_client.get(site_url(SITE_WITH_READINGS))).json()
        assert body["ingestion"]["collector"] is None


class TestMesuresDegradees:
    """Que vaut ce qui arrive, et l'a-t-on seulement regardé ?"""

    async def test_les_comptes_suivent_la_definition_de_letl(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        quality = (await api_client.get(site_url(FRESH_SITE))).json()["quality"]
        assert quality["total"] == FRESH_TOTAL
        assert quality["degraded"] == FRESH_DEGRADED
        assert quality["degraded_ratio"] == pytest.approx(
            FRESH_DEGRADED / FRESH_TOTAL
        )

    async def test_une_valeur_reconstruite_compte_comme_degradee(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Elle n'est ni qualifiée critical ni porteuse d'un motif de panne :
        # aucun des deux autres symptômes ne l'attraperait.
        quality = (await api_client.get(site_url(FRESH_SITE))).json()["quality"]
        assert quality["imputed"] == FRESH_IMPUTED
        assert quality["degraded"] > quality["sensor_failure"]

    async def test_le_vocabulaire_de_la_source_est_reconnu(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # `consumption_sensor_failure` vient de la source. L'ETL, lui, écrit
        # `<colonne>:undeclared` quand la source n'a rien déclaré : ne
        # reconnaître qu'un des deux laisserait passer la panne que personne
        # n'a étiquetée.
        quality = (await api_client.get(site_url(FRESH_SITE))).json()["quality"]
        assert quality["sensor_failure"] == FRESH_SENSOR_FAILURES

    async def test_un_motif_qui_nest_pas_une_panne_capteur_ne_compte_pas(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # `consumption_kw:sensor_timeout` contient « sensor » sans être ni le
        # vocabulaire de la source ni celui de l'ETL. Le compter ferait du
        # marqueur une recherche de sous-chaîne approximative.
        quality = (await api_client.get(site_url(FRESH_SITE))).json()["quality"]
        assert quality["sensor_failure"] < quality["not_good"]

    async def test_la_couverture_etl_est_publiee(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Le chiffre le plus important du bloc. `data_quality` vaut good par
        # défaut : sans lui, une fenêtre fraîchement collectée afficherait
        # 0 % de dégradation et le site paraîtrait parfait.
        quality = (await api_client.get(site_url(FRESH_SITE))).json()["quality"]
        assert quality["qualified"] == FRESH_QUALIFIED
        assert quality["qualified_ratio"] == pytest.approx(
            FRESH_QUALIFIED / FRESH_TOTAL
        )
        assert quality["qualified_ratio"] < 1.0

    async def test_une_fenetre_vide_ne_divise_pas_par_zero(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        quality = (await api_client.get(site_url(NEVER_COLLECTED_SITE))).json()[
            "quality"
        ]
        assert quality["total"] == 0
        assert quality["degraded_ratio"] == 0.0
        # Le zéro est ambigu, et c'est la couverture qui le lève : elle vaut
        # zéro aussi, donc rien n'a été qualifié, donc rien n'est établi.
        assert quality["qualified_ratio"] == 0.0
        assert quality["exceeds_threshold"] is False

    async def test_la_fenetre_est_celle_demandee(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Une fenêtre d'une heure exclut les deux heures pleines du jeu : seule
        # la mesure posée à l'instant reste.
        body = (
            await api_client.get(site_url(FRESH_SITE), params={"window_hours": 1})
        ).json()
        assert body["window_hours"] == 1
        assert body["quality"]["total"] < FRESH_TOTAL
        start = datetime.fromisoformat(body["quality"]["window_start"])
        end = datetime.fromisoformat(body["quality"]["window_end"])
        assert end - start == timedelta(hours=1)


class TestEcartPrevisionReel:
    """Le modèle qui s'appuie sur ces mesures tient-il encore ?"""

    async def test_les_previsions_sont_appariees_a_lheure(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # La source produit à la minute, le modèle prédit à l'heure : les
        # mesures sont moyennées par heure. Retenir la mesure la plus proche
        # ferait dépendre l'indicateur d'une seule minute.
        accuracy = (await api_client.get(site_url(FRESH_SITE))).json()["accuracy"]
        assert accuracy["paired_points"] == FRESH_PAIRED_POINTS
        assert accuracy["mean_actual_kw"] == pytest.approx(FRESH_MEAN_ACTUAL_KW)

    async def test_une_prevision_sans_mesure_ne_forme_pas_de_paire(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Le jeu porte trois prévisions et deux heures mesurées. Une paire
        # absente ne doit pas peser zéro dans une moyenne : elle doit ne pas
        # exister.
        accuracy = (await api_client.get(site_url(FRESH_SITE))).json()["accuracy"]
        assert accuracy["paired_points"] == FRESH_PAIRED_POINTS
        assert accuracy["mae_kw"] == pytest.approx(FRESH_MAE_KW)

    async def test_le_biais_est_signe(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Une surestimation de 10 kW et une sous-estimation de 10 kW donnent
        # une erreur absolue de 10 et un biais nul. C'est le biais qui dit la
        # direction, ce qu'une erreur absolue ne peut pas dire.
        accuracy = (await api_client.get(site_url(FRESH_SITE))).json()["accuracy"]
        assert accuracy["bias_kw"] == pytest.approx(FRESH_BIAS_KW)
        assert accuracy["mae_kw"] == pytest.approx(FRESH_MAE_KW)

    async def test_la_couverture_de_lintervalle_ignore_les_previsions_sans_bornes(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Une prévision sans intervalle n'est pas un raté de l'intervalle. La
        # compter comme telle ferait chuter la couverture à chaque version qui
        # ne déclare pas sa dispersion.
        accuracy = (await api_client.get(site_url(FRESH_SITE))).json()["accuracy"]
        assert accuracy["bounded_points"] == 1
        assert accuracy["within_bounds_ratio"] == pytest.approx(1.0)

    async def test_les_versions_comparees_sont_nommees(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Plus d'une entrée signale une fenêtre à cheval sur une promotion :
        # l'écart mêle alors deux modèles, et il faut pouvoir le voir.
        accuracy = (await api_client.get(site_url(FRESH_SITE))).json()["accuracy"]
        assert accuracy["model_versions"]

    async def test_un_site_sans_prevision_archivee_ne_conclut_rien(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # C'est le cas courant tant qu'aucun modèle n'est promu. Les autres
        # blocs doivent rester servis : une prévision absente n'efface pas la
        # fraîcheur d'ingestion.
        body = (await api_client.get(site_url(NEVER_COLLECTED_SITE))).json()
        accuracy = body["accuracy"]
        assert accuracy["paired_points"] == 0
        assert accuracy["mae_kw"] is None
        assert accuracy["drift"] is False
        assert body["ingestion"]["collector"] is not None


class TestDerive:
    """Le verdict est un rapport, jamais des kilowatts."""

    def test_une_erreur_sous_le_seuil_nest_pas_une_derive(self) -> None:
        assert is_drifting(mae_kw=10.0, mean_actual_kw=100.0, drift_mae_ratio=0.15) is (
            False
        )

    def test_une_erreur_au_dela_du_seuil_est_une_derive(self) -> None:
        assert is_drifting(mae_kw=20.0, mean_actual_kw=100.0, drift_mae_ratio=0.15) is (
            True
        )

    def test_le_seuil_lui_meme_nest_pas_une_derive(self) -> None:
        # Le seuil est la part tolérée, donc encore acceptable.
        assert is_drifting(mae_kw=15.0, mean_actual_kw=100.0, drift_mae_ratio=0.15) is (
            False
        )

    def test_le_verdict_est_relatif_et_non_absolu(self) -> None:
        # Les mêmes 20 kW d'écart, sur un bureau qui consomme 100 kW en
        # moyenne et sur une usine qui en consomme 1000, ne disent pas la même
        # chose. Un seuil en kilowatts vaudrait pour l'un et serait absurde
        # pour l'autre : c'est tout l'objet du rapport.
        bureau = is_drifting(20.0, 100.0, drift_mae_ratio=0.15)
        usine = is_drifting(20.0, 1000.0, drift_mae_ratio=0.15)
        assert bureau is True
        assert usine is False

    def test_sans_paire_il_ny_a_pas_de_derive_a_annoncer(self) -> None:
        assert is_drifting(None, None, drift_mae_ratio=0.15) is False

    def test_un_site_eteint_ne_derive_pas(self) -> None:
        # Une consommation moyenne nulle donnerait un seuil de zéro kilowatt,
        # que la moindre erreur dépasserait : le dashboard annoncerait une
        # dérive du modèle là où il n'y a qu'un site à l'arrêt.
        assert is_drifting(0.5, 0.0, drift_mae_ratio=0.15) is False


class TestReponseComplete:
    """Les trois blocs sont toujours là, et se suffisent à eux-mêmes."""

    async def test_la_reponse_porte_les_trois_indicateurs(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        body = (await api_client.get(site_url(FRESH_SITE))).json()
        assert set(body) == {
            "site_id",
            "generated_at",
            "window_hours",
            "ingestion",
            "quality",
            "accuracy",
        }

    async def test_un_seul_present_pour_toute_la_reponse(
        self,
        api_client: AsyncClient,
        indicator_sites: datetime,
    ) -> None:
        # Les trois agrégats et les âges publiés partagent le même instant :
        # sans cela, une même réponse décrirait deux présents légèrement
        # différents, et la fenêtre ne finirait pas où l'âge est mesuré.
        body = (await api_client.get(site_url(FRESH_SITE))).json()
        generated_at = datetime.fromisoformat(body["generated_at"])
        assert generated_at == datetime.fromisoformat(body["quality"]["window_end"])
        assert generated_at == datetime.fromisoformat(body["accuracy"]["window_end"])
        assert generated_at <= datetime.now(UTC)

    async def test_le_referentiel_reste_intact_apres_les_tests(
        self,
        db_session: AsyncSession,
        api_client: AsyncClient,
    ) -> None:
        # La fixture d'indicateurs ajoute deux sites et doit les retirer : les
        # tests d'EV-11 comptent le référentiel au site près.
        body = (await api_client.get("/api/v1/sites")).json()
        assert FRESH_SITE not in [entry["site_id"] for entry in body]
