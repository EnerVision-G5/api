# api

[![ci](https://github.com/EnerVision-G5/api/actions/workflows/ci.yml/badge.svg)](https://github.com/EnerVision-G5/api/actions/workflows/ci.yml)

API métier EnerVision, on-premise. Sert les sites, les mesures énergétiques et
les alertes sous JWT.

À ce stade le repo contient :

- le **contrat d'interface** (ticket EV-06) : les DTO Pydantic et les routes qui
  les déclarent. Tous les endpoints non triviaux renvoient `501 Not Implemented`.
  L'implémentation est portée par EV-11 (lecture des mesures) et EV-12
  (authentification JWT) ;
- le **squelette d'application** (ticket EV-38) : configuration, accès base
  (SQLAlchemy + Alembic), outillage Docker, tests et CI ;
- les **endpoints de lecture des mesures** (ticket EV-11) : `GET /sites`,
  `GET /sites/{site_id}`, `GET /sites/{site_id}/readings` et
  `GET /sites/{site_id}/readings/latest`, branchés sur PostgreSQL /
  TimescaleDB en SQLAlchemy asynchrone (asyncpg). `POST /auth/token` (EV-12)
  et `GET /alerts` restent en `501`.

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
| `app/db/` | `Base` ORM et session asynchrone (`get_db`, moteur asyncpg) |
| `app/models/energy.py` | Modèles ORM `Site` et `Mesure`, reflet du schéma d'infra |
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
make test        # base de test + pytest
```

Ces deux commandes tournent dans la CI (job `lint-and-test` de
`.github/workflows/ci.yml`, qui fournit la base par un service container).

Les tests des endpoints de lecture interrogent une **vraie base** PostgreSQL /
TimescaleDB, pas un double : le tri sur hypertable, le `TEXT[]` de
`null_reasons`, le comptage exact et la traversée des `NULL` ne se vérifient
que contre le moteur. `make test` démarre donc `compose.test.yml` (image
`timescale/timescaledb:2.17.2-pg16`, port 5433, données en mémoire, rien à
nettoyer) avant de lancer pytest.

Pour lancer pytest depuis l'hôte plutôt que depuis le conteneur :

```bash
make test-db
TEST_DATABASE_URL=postgresql+asyncpg://enervision:enervision@localhost:5433/enervision_test pytest
make test-db-down
```

Le schéma de test est construit depuis les modèles ORM d'`app/models/energy.py`.
La source de vérité du schéma reste `enervision-db/initdb/` dans le repo
**infra** (`01_schema.sql`, plus `03_mesure_imputation.sql` pour les colonnes
d'imputation) : une divergence entre les deux est un bug des modèles, à
corriger ici.

## Authentification (frontière EV-11 / EV-12)

`AUTH_ENABLED` pilote la dépendance `get_current_user` d'`app/security.py` :

| Valeur | Comportement |
|---|---|
| `false` (défaut) | les lectures passent en anonyme, aucun jeton n'est exigé |
| `true` | elles répondent `501`, faute de vérification de jeton implémentée |

Un `200` sous un drapeau nommé « auth activée » laisserait croire à une
protection inexistante, et un `401` laisserait croire que le jeton fourni est
en cause : d'où le `501`. Le décodage du JWT, `POST /auth/token` et les rôles
sont le périmètre d'**EV-12**. Laisser `AUTH_ENABLED=false` tant qu'il n'est
pas livré, sans quoi la suite de tests échoue.

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
