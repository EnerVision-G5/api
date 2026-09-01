"""Application FastAPI de l'API métier EnerVision.

Ce module ne contient que la déclaration du contrat d'interface : routes,
modèles d'entrée et de sortie, codes d'erreur. Aucune logique métier ici.

Source de vérité du contrat. Toute modification exige une PR sur
enervision/docs/contracts et la relecture des trois consommateurs.
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.routers import alerts, auth, health, sites
from app.schemas.auth import UserOut

# Version du contrat gelé dans enervision/docs/contracts/openapi-api.json.
# Incrémentée en semver : patch pour une description, minor pour un champ
# optionnel ajouté, major pour un champ retiré ou renommé.
CONTRACT_VERSION = "1.0.0"

API_PREFIX = "/api/v1"

app = FastAPI(
    title="EnerVision API métier",
    version=CONTRACT_VERSION,
    description=(
        "Contrat de l'API métier on-premise : sites, mesures énergétiques et"
        " alertes, servis sous JWT. Les endpoints non triviaux renvoient 501"
        " tant que leur ticket d'implémentation n'est pas livré."
    ),
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
app.include_router(alerts.router, prefix=API_PREFIX)


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
