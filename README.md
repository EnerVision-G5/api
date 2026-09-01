# api

API métier EnerVision, on-premise. Sert les sites, les mesures énergétiques et
les alertes sous JWT.

À ce stade le repo contient :

- le **contrat d'interface** (ticket EV-06) : les DTO Pydantic et les routes qui
  les déclarent. Tous les endpoints non triviaux renvoient `501 Not Implemented`.
  L'implémentation est portée par EV-11 (lecture des mesures) et EV-12
  (authentification JWT) ;
- le **squelette d'application** (ticket EV-38) : configuration, accès base
  (SQLAlchemy + Alembic), outillage Docker, tests et CI.

## Démarrer

### Docker (recommandé)

```bash
cp .env.example .env          # ou : make env
docker compose -f compose.dev.yml up --build
```

- API : <http://localhost:8080>
- Health : <http://localhost:8080/api/v1/health>
- Documentation interactive : <http://localhost:8080/docs>

Le code est monté en volume, `uvicorn --reload` recharge à chaque modification.

### Sans Docker

```bash
python -m venv .venv
source .venv/bin/activate      # Windows : .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8080
```

## Structure

| Chemin | Rôle |
|---|---|
| `app/main.py` | Application FastAPI, `CONTRACT_VERSION`, préfixe `/api/v1` |
| `app/core/config.py` | Configuration (`pydantic-settings`, lecture `.env`) |
| `app/db/` | `Base` ORM et session SQLAlchemy (`get_db`) |
| `app/models/` | Modèles ORM (à compléter) |
| `app/schemas/common.py` | `ErrorResponse`, `PaginationMeta`, `DataQuality` |
| `app/schemas/energy.py` | `SiteOut`, `EnergyReadingOut`, `ReadingsPage`, `AlertOut` |
| `app/schemas/auth.py` | `TokenResponse`, `UserOut` |
| `app/security.py` | Schéma OAuth2 exposé dans la spécification |
| `app/routers/` | Routes, une par domaine, sans logique métier |
| `alembic/` | Migrations |
| `scripts/export_openapi.py` | Export de la spécification OpenAPI |
| `tests/` | pytest |

## Qualité

```bash
ruff check .     # lint   (make lint)
pytest           # tests  (make test)
```

Ces deux commandes tournent dans la CI (job `lint-and-test` de
`.github/workflows/ci.yml`).

## Migrations

```bash
make revision m="create site table"   # autogenerate
make migrate                          # alembic upgrade head
```

## Contrat OpenAPI

La source de vérité du contrat, ce sont les DTO de `app/schemas`. Le fichier
gelé qui fait foi entre les équipes est
`enervision/docs/contracts/openapi-api.json`.

Régénérer le contrat gelé après une évolution volontaire :

```bash
python scripts/export_openapi.py ../docs/contracts/openapi-api.json
```

Le job CI `contract-drift` compare à chaque push la spécification générée depuis
le code au fichier gelé et fait échouer le build en cas d'écart. La procédure
d'évolution est décrite dans `enervision/docs/contracts/README.md`.

Les versions de FastAPI, Pydantic et Uvicorn sont épinglées dans
`requirements.txt` pour que la spécification générée reste reproductible : ne
pas les bumper sans PR de contrat.

## Secrets

`.env` est ignoré par Git, seul `.env.example` est versionné.
