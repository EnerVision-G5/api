# EnerVision — Base TimescaleDB (schéma figé v1.0)

Cible stable pour l'ETL et l'API. Périmètre gelé : `mesure` (hypertable),
`mesure_exclu`, `prediction`, `modele`, `app_user` (+ `site`, référentiel
requis pour les clés étrangères).

## Démarrage

```bash
cp .env.example .env      # puis changer POSTGRES_PASSWORD
docker compose up -d
docker compose ps         # attendre l'état "healthy"
```

Les scripts de `initdb/` s'exécutent automatiquement **au premier démarrage
uniquement** (volume vide). Pour repartir de zéro : `docker compose down -v`.

## Vérification des critères d'acceptation

```bash
# 1. Extension TimescaleDB activée
docker compose exec timescaledb psql -U enervision -d enervision -c "\dx timescaledb"

# 2. Hypertable sur mesure
docker compose exec timescaledb psql -U enervision -d enervision \
  -c "SELECT hypertable_name, num_chunks FROM timescaledb_information.hypertables;"

# 3. Les 6 tables présentes
docker compose exec timescaledb psql -U enervision -d enervision -c "\dt"

# 4. Insertion de test (puis nettoyage)
docker compose exec timescaledb psql -U enervision -d enervision -c "
  INSERT INTO mesure (ts, site_id, consumption_kw, data_quality)
  VALUES (now(), 'SITE001', 87.34, 'good');
  SELECT * FROM mesure; DELETE FROM mesure;"
```

## Points de conception à relire (revue API + Data)

- **PK composite `(site_id, ts)` sur `mesure`** — imposé par TimescaleDB :
  toute contrainte unique d'une hypertable doit inclure la colonne de
  partitionnement. Conséquence pour l'ETL : idempotence via
  `ON CONFLICT (site_id, ts) DO NOTHING`.
- **`mesure_exclu` référence `(site_id, ts)`** — FK vers une hypertable,
  supportée depuis TimescaleDB 2.16 (image épinglée : 2.17.2).
- **Chunks de 7 jours** sur `mesure` (`chunk_time_interval`).
- **NULL conservés** avec `null_reasons` + `data_quality` (conseil ETL de
  la doc API : ne jamais filtrer les NULL).
- **`prediction` en table classique** — volumétrie faible ; conversion en
  hypertable possible plus tard par migration.
- **Seed `site`** : SITE004–007 sont des placeholders, l'ETL doit faire un
  UPSERT depuis `GET /api/v1/sites` au démarrage.

## Règle d'évolution

Le schéma est figé : toute modification passe par un nouveau fichier de
migration `initdb/NN_description.sql` (ou un outil type Flyway/Alembic),
jamais par l'édition de `01_schema.sql`.
