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
- les **prédictions** (ticket EV-38) : un job planifié interroge le service
  d'inférence et archive le résultat, que
  `GET /api/v1/sites/{site_id}/predictions` sert au dashboard.
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
| `app/jobs/predict_refresh.py` | Job de rafraîchissement des prédictions |
| `app/predict_client.py` | Client HTTP du service d'inférence |
| `app/predictions_store.py` | Archivage des prédictions |
| `app/timewindow.py` | Fenêtre de temps partagée par les routes paginées |
| `app/db/lookups.py` | Consultations de référentiel partagées |
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

## Prédictions

Le dashboard **ne déclenche jamais** une prédiction. Un job planifié interroge
le service d'inférence, archive le résultat, et le dashboard le relit :

```
cron  ──►  app.jobs.predict_refresh  ──►  Serving (POST /api/v1/predict)
                     │
                     ▼
                base ─────►  GET /api/v1/sites/{site_id}/predictions  ──►  dashboard
```

`POST /api/v1/predict` appartient à Serving seul : l'API en est cliente et ne
le réexpose pas.

### Le job

```bash
python -m app.jobs.predict_refresh
```

Destiné à un **conteneur cron dédié**, à la manière du Collector : aucun
ordonnanceur n'est embarqué dans le processus de l'API, ce qui la laisse sans
état et permet de rejouer le job à la main sans la redémarrer.

| Variable | Rôle |
|---|---|
| `DATABASE_URL` | base à alimenter |
| `PREDICT_URL` | adresse de Serving. Le chemin `/api/v1/predict` vient de son contrat et n'est pas configurable |
| `PREDICT_HORIZON_HOURS` | profondeur demandée, 24 par défaut (bornes du contrat : 1 à 48) |
| `PREDICT_TIMEOUT_SECONDS` | délai par site, 10 par défaut |

Il lit les sites **actifs**, appelle Serving pour chacun, archive les points,
et écrit un résumé d'une ligne par site :

```
SITE001 ok 24 points
SITE002 echec ReadTimeout: delai depasse
Bilan : 6/7 site(s) traité(s), 144 ligne(s) archivée(s).
```

**Un site en échec n'arrête pas la tournée.** Le code de sortie ne vaut `1`
que si *aucun* site n'a abouti : une panne isolée ne doit pas réveiller
l'astreinte, une panne générale doit le faire. Un référentiel sans site actif
compte aussi pour un échec, une base vide n'étant pas un succès.

**Idempotence** : la clé du schéma v1.0 est `(modele_id, site_id, ts_cible)`
et le conflit met la ligne à jour. Rejouer le job ne duplique rien, et une
nouvelle prévision du même instant par le même modèle **remplace** la
précédente, la plus fraîche étant celle qui vaut. `generated_at` suit la mise à
jour : c'est la date de production par Serving, distincte de `created_at` qui
date l'insertion.

**`model_version` n'est pas archivé**, et c'est voulu : la valeur est celle de
`modele.version`, atteinte par la jointure sur `modele_id`. Le service
d'inférence la lit dans le registre MLflow et l'entraînement l'y repose à
chaque promotion — la dupliquer ouvrirait deux valeurs pour un même fait, sans
arbitre. Le job traduit donc la version annoncée en clé du registre, et
**échoue ce site** s'il ne la résout pas : `prediction.modele_id` est `NOT
NULL`, il n'y a rien à quoi rattacher la ligne. Deux cas, distingués dans les
logs — version absente du registre (le service sert un modèle chargé par
chemin d'artefact plutôt que par alias), ou version portée par plusieurs
modèles sans qu'un seul soit actif, la clé de `modele` étant `(nom, version)`
que le contrat ne transporte pas.

### La route de lecture

```bash
curl -s -H "Authorization: Bearer $TOKEN"      "http://localhost:8080/api/v1/sites/SITE001/predictions?start_time=2026-09-03T00:00:00Z&end_time=2026-09-04T00:00:00Z"
```

Symétrique de `/readings` : mêmes paramètres, `start_time` et `end_time`
requis, `limit` de 1 à 1000 par défaut 100, `offset` par défaut 0, tri par
horodatage croissant, `meta.total` exact sur la fenêtre. La fenêtre de temps et
la vérification d'existence du site sont **partagées** entre les deux routes,
pour qu'un site inconnu ou des bornes inversées répondent la même chose des
deux côtés.

La fenêtre porte sur l'horodatage **cible**, pas sur la date de génération : le
client demande « que prévoit-on pour telle période ». Il y a une ligne par
modèle et par instant cible, celle de la prévision la plus fraîche ; deux
modèles distincts prédisant le même instant donnent deux lignes, que leur
`model_version` distingue.

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
