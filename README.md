# api

[![ci](https://github.com/EnerVision-G5/api/actions/workflows/ci.yml/badge.svg)](https://github.com/EnerVision-G5/api/actions/workflows/ci.yml)

API métier EnerVision, on-premise. Sert les sites, les mesures énergétiques et
les alertes sous JWT.

À ce stade (ticket EV-06) le repo ne contient que le **contrat d'interface** :
les DTO Pydantic et les routes qui les déclarent. Tous les endpoints non
triviaux renvoient `501 Not Implemented`. L'implémentation est portée par
EV-11 (lecture des mesures) et EV-12 (authentification JWT).

## Démarrer

```bash
python -m venv .venv
.venv/Scripts/activate      # Linux et macOS : source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

La documentation interactive est sur <http://127.0.0.1:8000/docs>.

## Structure

| Chemin | Rôle |
|---|---|
| `app/schemas/common.py` | `ErrorResponse`, `PaginationMeta`, `DataQuality` |
| `app/schemas/energy.py` | `SiteOut`, `EnergyReadingOut`, `ReadingsPage`, `AlertOut` |
| `app/schemas/auth.py` | `TokenResponse`, `UserOut` |
| `app/security.py` | Déclaration du schéma OAuth2 exposé dans la spécification |
| `app/routers/` | Routes, une par domaine, sans logique métier |
| `app/main.py` | Application FastAPI et `CONTRACT_VERSION` |
| `scripts/export_openapi.py` | Export de la spécification OpenAPI |

## Contrat OpenAPI

La source de vérité du contrat, ce sont les DTO de `app/schemas`. Le fichier
gelé qui fait foi entre les équipes est
`enervision/docs/contracts/openapi-api.json`.

Régénérer le contrat gelé après une évolution volontaire :

```bash
python scripts/export_openapi.py ../docs/contracts/openapi-api.json
```

La CI exécute le job `contract-drift` à chaque push. Il compare la
spécification générée depuis le code au fichier gelé et fait échouer le build
en cas d'écart. La procédure d'évolution est décrite dans
`enervision/docs/contracts/README.md`.

Les versions de FastAPI et de Pydantic sont épinglées dans `requirements.txt`
pour que la spécification générée reste reproductible.
