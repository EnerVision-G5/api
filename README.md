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
  TimescaleDB en SQLAlchemy asynchrone (asyncpg) ;
- l'**authentification JWT** (ticket EV-12) : `POST /auth/token` délivre un
  jeton, les lectures l'exigent, et `require_role` est prête pour les futures
  écritures ;
- le **proxy des prédictions** (ticket EV-38) : `POST /api/v1/predict` relaie
  la demande au service d'inférence et archive ce qui est archivable.
  `GET /alerts` reste en `501`.

## Démarrer

### Docker (recommandé)

```bash
cp .env.example .env          # ou : make env
# Puis renseigner JWT_SECRET dans .env, sinon l'API refuse de demarrer :
python -c "import secrets; print(secrets.token_urlsafe(48))"
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
# JWT_SECRET est vide dans .env.example : en generer un avant de demarrer.
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
| `app/models/user.py` | Modèle ORM `AppUser` (comptes locaux, rôles) |
| `app/password.py` | Hachage argon2id (seul module à le manipuler) |
| `app/predict_client.py` | Client HTTP du service d'inférence |
| `app/predictions_store.py` | Archivage des prédictions servies |
| `app/db/lookups.py` | Consultations de référentiel partagées (existence d'un site) |
| `app/security.py` | JWT, `get_current_user`, `require_role` |
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

## Authentification

Flux OAuth2 mot de passe et jetons JWT signés en HS256, conformément à
ADR-009 : aucun état côté serveur, le jeton voyage dans l'en-tête
`Authorization`.

```bash
# 1. Obtenir un jeton
curl -s -X POST http://localhost:8080/api/v1/auth/token      -d 'username=dev.reader&password=changeme-dev' | jq -r .access_token

# 2. L'utiliser
curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/sites
```

Les comptes de développement `dev.reader` et `dev.writer` viennent du seed
`enervision-db/dev-seed/dev_users.py` du repo **infra**, à appliquer
explicitement : il est hors d'`initdb/` pour ne jamais atterrir en production.

### Configuration

| Variable | Rôle |
|---|---|
| `JWT_SECRET` | clé de signature, **32 caractères minimum**. L'API refuse de démarrer si elle est absente ou trop courte. Jamais versionnée |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | durée de vie, 60 par défaut |
| `AUTH_ENABLED` | `true` par défaut. `false` repasse les endpoints en anonyme |

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`AUTH_ENABLED=false` reste offert pour appeler l'API sans se connecter en
développement local. **À ne jamais déployer** : dans ce mode
`get_current_user` rend `None` sans rien vérifier, et `require_role` laisse
passer, faute d'utilisateur identifiable.

### Hachage des mots de passe

**argon2id**, via `argon2-cffi`, dans le seul module `app/password.py` :
recommandation OWASP de premier choix, résistance au matériel dédié par le
coût mémoire là où bcrypt ne coûte que du temps, et écosystème maintenu — à la
différence de `passlib`, retiré du projet, qui n'est plus maintenu et se
brouille avec les versions récentes de `bcrypt`.

Les paramètres sont ceux d'`argon2-cffi` par défaut (`t=3`, `m=64 MiB`, `p=4`),
au-dessus des minimums OWASP. Une connexion réussie dont le hachage a été
produit avec des paramètres dépassés le remplace au passage : c'est le seul
instant où l'API détient le mot de passe en clair.

Aucun autre module ne manipule un hachage, et il n'existe qu'un seul
mécanisme : pas de cohabitation bcrypt / argon2.

### Ce que fait la vérification

`get_current_user` contrôle la signature et l'expiration, lit `sub`, recharge
l'utilisateur en base et refuse en `401` avec `WWW-Authenticate: Bearer` dans
tous les autres cas. Le **rôle exposé est celui de la base**, pas celui du
claim : un privilège retiré prend effet sans attendre l'expiration du jeton.
Le claim `role` reste présent pour que le dashboard connaisse le sien sans
appel supplémentaire, le contrat ne publiant aucun endpoint de profil.

Un compte inconnu et un mot de passe faux renvoient le même message, et le
hachage est vérifié dans les deux cas pour que le temps de réponse ne révèle
pas quels comptes existent.

### Rôles

`reader` et `writer`, les valeurs de `UserOut.role` au contrat.
`require_role("writer")` est livrée prête pour les futurs endpoints
d'écriture ; **aucun endpoint du contrat gelé ne l'utilise à ce jour**, en
ajouter un ferait dériver la spécification. Elle refuse en `403`, et non en
`401` : l'appelant est authentifié, simplement pas autorisé.

Hors périmètre à ce stade : pas de rate limiting sur `/auth/token`, pas de
jeton de rafraîchissement, aucune gestion des utilisateurs par l'API.

## Prédictions (proxy)

Le dashboard n'appelle plus le service d'inférence directement : il passe par
`POST /api/v1/predict`, qui relaie vers Serving sur `ml_network` et rend la
réponse **sans la transformer**.

```bash
curl -s -X POST http://localhost:8080/api/v1/predict      -H "Authorization: Bearer $TOKEN"      -H 'Content-Type: application/json'      -d '{"site_id": "SITE001", "horizon_hours": 24}'
```

| Variable | Rôle |
|---|---|
| `PREDICT_URL` | adresse de Serving. Le chemin `/api/v1/predict` vient de son contrat et n'est pas configurable. Sans valeur, la route répond `503` |
| `PREDICT_TIMEOUT_SECONDS` | délai total de l'appel, 3 s par défaut |

### Séquence et codes d'erreur

1. authentification (`401` sans jeton valide) ;
2. validation du corps (`422`) ;
3. **vérification du site en base** (`404`) — Serving n'est pas appelé, un site
   inconnu est une erreur du client et le message est celui des routes de
   lecture ;
4. appel de Serving (`503` sur délai dépassé, erreur de connexion, réponse non
   `200`, ou réponse `200` hors contrat) ;
5. archivage au mieux, puis réponse.

Le `503` porte un message générique : le détail de la panne va dans les logs,
pas au client.

### Archivage partiel, et pourquoi

La table `prediction` ne porte **ni** `lower_bound_kw`, **ni**
`upper_bound_kw`, **ni** `model_version`, **ni** `generated_at`. Sont archivés
le site, l'horodatage cible, la valeur prédite et le modèle résolu. C'est une
décision d'équipe assumée : persistance minimale, sans migration de schéma.

L'archivage est **au mieux**. `store_prediction` ne lève jamais : la prédiction
est déjà obtenue et due au client, aucun échec d'écriture ne la lui retire.
Chaque échec est journalisé.

Deux cas où rien n'est archivé alors que la réponse part normalement :

- **modèle absent du registre** — `modele` est alimenté par l'équipe Data,
  jamais par l'API, et reste vide à ce jour ;
- **résolution ambiguë** — la clé unique de `modele` est `(nom, version)` et le
  contrat ne transporte que `model_version`. Si deux modèles partagent la
  version, en choisir un serait deviner.

Un même instant cible réévalué remplace la valeur précédente : la prédiction la
plus fraîche est celle que le dashboard affiche.

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

Les mots de passe sont hachés en argon2id, jamais stockés en clair, y compris
dans les jeux de test et le seed de développement.

`.env` est ignoré par Git, seul `.env.example` est versionné, et `JWT_SECRET`
y est **volontairement vide** : aucune clé de signature ne doit exister dans
le dépôt. En production elle vient de Key Vault (ADR-013).

L'API valide la clé au démarrage plutôt qu'à la première requête : mieux vaut
ne pas démarrer du tout que servir des jetons forgeables.
