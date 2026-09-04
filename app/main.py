"""Application FastAPI de l'API métier EnerVision.

Ce module ne contient que la déclaration du contrat d'interface : routes,
modèles d'entrée et de sortie, codes d'erreur. Aucune logique métier ici.

Source de vérité du contrat. Toute modification exige une PR sur
enervision/docs/contracts et la relecture des trois consommateurs.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.core.config import check_cors_origins, get_cors_origins, get_jwt_secret
from app.routers import (
    alerts,
    auth,
    health,
    indicators,
    models,
    predictions,
    sites,
    source_relay,
)
from app.schemas.auth import UserOut

# Version du contrat gelé dans enervision/docs/contracts/openapi-api.json.
# Incrémentée en semver : patch pour une description, minor pour un champ
# optionnel ajouté, major pour un champ retiré ou renommé.
#
# 1.3.0 : trois routes additives relayant la source — déclenchement d'un pic,
# historique des pics, synchronisation du référentiel. Les deux écritures
# sont les premières du contrat à exiger le rôle writer.
#
# 1.4.0 : GET /alerts cesse de répondre 501 et sert la table `alerte` ;
# GET /sites/{id}/sensors expose la santé des capteurs ; EnergyReadingOut
# gagne `excluded` et `exclusion_reason`, qui rendent enfin lisible la mise
# à l'écart que `mesure_exclu` portait sans que rien ne la publie.
#
# Un assouplissement, et il est délibéré : AlertOut.value et .threshold
# passent nullables. Ils étaient déclarés requis par un contrat qu'aucune
# réponse n'avait jamais honoré, l'endpoint répondant 501 depuis l'origine.
# Perdre une alerte parce qu'il lui manque un chiffre serait pire que la
# servir sans.
#
# 1.5.0 : quatre routes additives. L'historique des pannes de capteur, que
# /sensors ne pouvait pas donner en ne gardant que le présent ; le registre
# des modèles et la version promue, en base depuis le schéma v1.0 sans que
# rien ne les lise ; et les recommandations d'action d'EV-32, que le
# dashboard attendait sans les anticiper.
CONTRACT_VERSION = "1.5.0"

API_PREFIX = "/api/v1"

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Valide la configuration de signature avant d'accepter le trafic.

    Démarrer avec une clé absente ou trop courte donnerait une API qui
    répond, délivre des jetons et les accepte, tout en étant forgeable : il
    vaut mieux ne pas démarrer du tout et le dire. La vérification est ici,
    et non à l'import, pour que l'export du contrat OpenAPI et les tests qui
    ne touchent pas à l'authentification n'exigent aucun secret.
    """
    get_jwt_secret()
    check_cors_origins()
    yield


app = FastAPI(
    title="EnerVision API métier",
    version=CONTRACT_VERSION,
    lifespan=lifespan,
    description=(
        "Contrat de l'API métier on-premise : sites, mesures énergétiques et"
        " alertes, servis sous JWT. Les endpoints non triviaux renvoient 501"
        " tant que leur ticket d'implémentation n'est pas livré."
    ),
)


# Le dashboard est servi depuis une autre origine que l'API : sans ces
# en-têtes, le navigateur refuse ses requêtes avant même qu'elles partent.
#
# La liste est lue au montage, une fois : changer les origines demande un
# redémarrage, ce qui est le cas de toute la configuration. Aucun joker n'est
# accepté, et allow_credentials va de pair avec des origines nommées — le
# jeton voyage dans l'en-tête Authorization, pas dans un cookie, mais laisser
# la porte ouverte à un site tiers reviendrait à lui offrir les réponses de
# l'API pour tout utilisateur déjà connecté.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(RequestValidationError)
def validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Aligne les erreurs de validation sur le modèle ErrorResponse.

    Sans ce gestionnaire, FastAPI renverrait un HTTPValidationError dont le
    champ detail est une liste, ce qui contredirait le contrat annoncé pour
    les réponses 422.
    """
    detail = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": detail or "Requête invalide."},
    )


app.include_router(health.router, prefix=API_PREFIX)
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(sites.router, prefix=API_PREFIX)
app.include_router(predictions.router, prefix=API_PREFIX)
app.include_router(alerts.router, prefix=API_PREFIX)
app.include_router(models.router, prefix=API_PREFIX)
# Après sites.py, et sans conséquence : la collection des indicateurs est à
# /indicators, au premier niveau. Un chemin littéral sous /sites serait avalé
# par /sites/{site_id}, déclaré plus haut — voir le module.
app.include_router(indicators.router, prefix=API_PREFIX)
# Après sites.py, et ça compte : POST /sites/sync est un chemin littéral, que
# GET /sites/{site_id} n'avale pas puisque la méthode diffère, mais l'ordre
# reste celui qui se lit — le littéral après le paramétré fonctionne ici,
# l'inverse serait à vérifier à chaque ajout.
app.include_router(source_relay.router, prefix=API_PREFIX)


def _normalize_error_responses(spec: dict[str, Any]) -> dict[str, Any]:
    """Aligne toutes les réponses 422 sur le modèle ErrorResponse.

    FastAPI documente spontanément un HTTPValidationError dont le champ detail
    est une liste. Le gestionnaire ci-dessus renvoyant un ErrorResponse, la
    spécification doit dire la même chose que le runtime, sinon le contrat gelé
    décrirait une forme d'erreur que l'API ne produit jamais.
    """
    error_ref = {"$ref": "#/components/schemas/ErrorResponse"}
    for operations in spec.get("paths", {}).values():
        for operation in operations.values():
            response = operation.get("responses", {}).get("422")
            if response is None:
                continue
            response["description"] = "Paramètres de requête invalides."
            response["content"] = {"application/json": {"schema": error_ref}}
    schemas = spec.get("components", {}).get("schemas", {})
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    # UserOut fait partie du contrat d'authentification (EV-06) mais aucun
    # endpoint ne le renvoie encore : il est publié explicitement pour que le
    # dashboard en dérive un type dès maintenant, sans attendre EV-12.
    schemas.setdefault("UserOut", UserOut.model_json_schema())
    return spec


def custom_openapi() -> dict[str, Any]:
    """Spécification OpenAPI servant de source unique au contrat gelé."""
    if app.openapi_schema is None:
        app.openapi_schema = _normalize_error_responses(
            get_openapi(
                title=app.title,
                version=CONTRACT_VERSION,
                description=app.description,
                routes=app.routes,
            )
        )
    return app.openapi_schema


app.openapi = custom_openapi
