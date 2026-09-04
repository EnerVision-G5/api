"""Le registre des modèles, enfin lisible.

La table `modele` existait depuis le schéma figé v1.0 et n'était atteinte que
par une jointure interne, pour retrouver `model_version` sur une prévision.
Rien ne permettait de répondre à « quel modèle sert en ce moment », ni de voir
ce qui a servi avant lui — deux questions qu'on se pose exactement le jour où
une prévision paraît fausse.

Le jeu de test porte DEUX versions promues, et ce n'est pas une erreur : le
schéma ne contraint pas l'unicité de `actif`. La route doit donc trancher, et
la façon dont elle tranche est la seule décision de ce module.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import ARBITRATED_MODEL_NAME, KNOWN_MODEL_NAME

pytestmark = pytest.mark.anyio

MODELS_URL = "/api/v1/models"
CURRENT_URL = "/api/v1/models/current"


class TestRegistre:
    async def test_les_modeles_sortent_du_plus_recent_au_plus_ancien(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(MODELS_URL)

        assert response.status_code == 200
        rows = response.json()
        assert len(rows) == 5
        dates = [row["created_at"] for row in rows]
        assert dates == sorted(dates, reverse=True)

    async def test_chaque_modele_porte_de_quoi_le_tracer(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(MODELS_URL, params={"nom": KNOWN_MODEL_NAME})

        row = response.json()[0]
        assert row["nom"] == KNOWN_MODEL_NAME
        assert row["version"]
        assert row["actif"] is True
        # `date_entrainement` date le run MLflow, `created_at` l'entrée dans le
        # registre. Les deux champs existent parce qu'ils ne disent pas la
        # même chose, et le jeu de test laisse le premier nul.
        assert "date_entrainement" in row
        assert "mlflow_run_id" in row

    async def test_le_filtre_actif_isole_les_versions_promues(
        self,
        api_client: AsyncClient,
    ) -> None:
        promus = await api_client.get(MODELS_URL, params={"actif": True})
        retires = await api_client.get(MODELS_URL, params={"actif": False})

        assert {row["nom"] for row in promus.json()} == {
            KNOWN_MODEL_NAME,
            ARBITRATED_MODEL_NAME,
        }
        assert all(row["actif"] is False for row in retires.json())

    async def test_le_registre_exige_un_jeton(
        self,
        anonymous_client: AsyncClient,
    ) -> None:
        response = await anonymous_client.get(MODELS_URL)

        assert response.status_code == 401


class TestModelePromu:
    async def test_le_modele_promu_est_servi_seul(
        self,
        api_client: AsyncClient,
    ) -> None:
        response = await api_client.get(CURRENT_URL)

        assert response.status_code == 200
        assert response.json()["actif"] is True

    async def test_current_n_est_pas_lu_comme_un_identifiant(
        self,
        api_client: AsyncClient,
    ) -> None:
        """Le chemin littéral est déclaré avant tout chemin paramétré.

        L'ordre inverse ferait avaler « current » par `/{modele_id}` le jour
        où cette route existera, et la réponse deviendrait un 404 sur un
        identifiant introuvable.
        """
        response = await api_client.get(CURRENT_URL)

        assert response.status_code == 200
        assert "modele_id" in response.json()

    async def test_le_modele_promu_exige_un_jeton(
        self,
        anonymous_client: AsyncClient,
    ) -> None:
        response = await anonymous_client.get(CURRENT_URL)

        assert response.status_code == 401
