-- ============================================================
-- Projet EnerVision — Schéma figé v1.0 (TimescaleDB / PG16)
-- Périmètre gelé : mesure, mesure_exclu, prediction, modele, app_user
-- (+ site, référentiel requis pour l'intégrité des FK)
--
-- Exécuté automatiquement au premier démarrage du conteneur
-- via /docker-entrypoint-initdb.d (ordre alphabétique).
-- Toute évolution ultérieure = nouveau fichier de migration,
-- jamais de modification de ce fichier (schéma relu par API et Data).
-- ============================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ------------------------------------------------------------
-- SITE — référentiel des sites (GET /api/v1/sites)
-- ------------------------------------------------------------
CREATE TABLE site (
    site_id       VARCHAR(20)  PRIMARY KEY,           -- ex. 'SITE001'
    site_type     VARCHAR(50)  NOT NULL,              -- office / factory / ...
    site_name     VARCHAR(150) NOT NULL,
    location      VARCHAR(150),
    capacity_kw   NUMERIC(10,2) NOT NULL CHECK (capacity_kw > 0),
    status        VARCHAR(20)  NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'inactive'))
);

-- ------------------------------------------------------------
-- APP_USER — utilisateurs authentifiés via OAuth2
-- ("user" est un mot réservé SQL, d'où app_user)
-- Aucun mot de passe stocké : l'identité vient du provider.
-- ------------------------------------------------------------
CREATE TABLE app_user (
    user_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    oauth_provider     VARCHAR(50)  NOT NULL,          -- ex. 'keycloak', 'google'
    oauth_subject      VARCHAR(255) NOT NULL,          -- claim "sub" du JWT
    email              VARCHAR(255) NOT NULL,
    display_name       VARCHAR(150),
    role               VARCHAR(20)  NOT NULL DEFAULT 'viewer'
                       CHECK (role IN ('admin', 'analyst', 'viewer')),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_login_at      TIMESTAMPTZ,
    UNIQUE (oauth_provider, oauth_subject)
);

-- ------------------------------------------------------------
-- MESURE — lectures brutes (EnergyReading), HYPERTABLE
-- Contrainte TimescaleDB : toute PK / contrainte unique doit
-- inclure la colonne de partitionnement (ts), d'où la PK
-- composite (site_id, ts) au lieu d'un id auto-incrémenté.
-- Les NULL sont conservés tels quels avec data_quality.
-- ------------------------------------------------------------
CREATE TABLE mesure (
    ts                   TIMESTAMPTZ   NOT NULL,
    site_id              VARCHAR(20)   NOT NULL REFERENCES site(site_id),
    consumption_kw       NUMERIC(10,2),                -- nullable (panne capteur)
    consumption_kwh      NUMERIC(10,2),
    voltage_v            NUMERIC(8,2),
    current_a            NUMERIC(8,2),
    power_factor         NUMERIC(4,3) CHECK (power_factor BETWEEN 0 AND 1),
    temperature_celsius  NUMERIC(5,2),
    humidity_percent     NUMERIC(5,2) CHECK (humidity_percent BETWEEN 0 AND 100),
    null_reasons         TEXT[]        NOT NULL DEFAULT '{}',
    data_quality         VARCHAR(10)   NOT NULL DEFAULT 'good'
                         CHECK (data_quality IN ('good', 'partial', 'degraded', 'critical')),
    inserted_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, ts)
);

-- Hypertable partitionnée par temps, chunks de 7 jours
-- (~10k lignes/jour à 7 sites × 1 mesure/min : chunks raisonnables)
SELECT create_hypertable('mesure', 'ts', chunk_time_interval => INTERVAL '7 days');

-- La PK (site_id, ts) couvre les requêtes par site ;
-- index dédié pour l'audit qualité des capteurs
CREATE INDEX idx_mesure_quality ON mesure (data_quality, ts DESC)
    WHERE data_quality <> 'good';

-- ------------------------------------------------------------
-- MESURE_EXCLU — mesures écartées des calculs agrégés
-- Référence la mesure d'origine par sa clé naturelle (site_id, ts).
-- NB : la FK vers une hypertable nécessite TimescaleDB >= 2.16
-- (OK avec l'image latest-pg16). Si version antérieure :
-- supprimer la contrainte FOREIGN KEY et garder le UNIQUE.
-- ------------------------------------------------------------
CREATE TABLE mesure_exclu (
    exclusion_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id       VARCHAR(20)  NOT NULL,
    ts            TIMESTAMPTZ  NOT NULL,
    raison        TEXT         NOT NULL,               -- ex. 'network_loss', 'valeur aberrante'
    exclu_par     BIGINT       REFERENCES app_user(user_id),  -- NULL = exclusion automatique
    exclu_le      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (site_id, ts),                              -- une mesure exclue une seule fois
    FOREIGN KEY (site_id, ts) REFERENCES mesure(site_id, ts)
);

-- ------------------------------------------------------------
-- MODELE — registre des modèles de prédiction
-- (miroir applicatif du Model Registry MLflow)
-- ------------------------------------------------------------
CREATE TABLE modele (
    modele_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nom               VARCHAR(100) NOT NULL,            -- ex. 'baseline', 'enervision_xgboost'
    version           VARCHAR(20)  NOT NULL,            -- ex. '1'
    mlflow_run_id     VARCHAR(64),                      -- traçabilité vers le run MLflow
    date_entrainement TIMESTAMPTZ,
    actif             BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (nom, version)
);

-- ------------------------------------------------------------
-- PREDICTION — sorties des modèles par site et horodatage cible
-- Table classique (volumétrie faible vs mesure) ; pourra être
-- convertie en hypertable par migration si le besoin apparaît.
-- ------------------------------------------------------------
CREATE TABLE prediction (
    prediction_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    modele_id              BIGINT        NOT NULL REFERENCES modele(modele_id),
    site_id                VARCHAR(20)   NOT NULL REFERENCES site(site_id),
    ts_cible               TIMESTAMPTZ   NOT NULL,
    consumption_kw_predite NUMERIC(10,2) NOT NULL,
    created_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),
    UNIQUE (modele_id, site_id, ts_cible)
);

CREATE INDEX idx_prediction_site_ts ON prediction (site_id, ts_cible DESC);
