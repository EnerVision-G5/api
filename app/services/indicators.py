"""Calcul des indicateurs de confiance dans la donnée (EV-18).

Trois agrégats, un par indicateur, et une seule règle de découpe : le SQL est
ici, les décisions de forme sont dans les DTO, le routeur n'assemble. Les
trois fonctions publiques travaillent sur un ENSEMBLE de sites et non sur un
site, y compris quand l'appelant n'en veut qu'un : c'est ce qui permet à la
route de collection de répondre en trois requêtes au lieu de trois par site,
et surtout ce qui garantit que les deux routes calculent la même chose.

Les seuils ne sont pas ici. Ils viennent de Settings, et sont republiés dans
la réponse à côté de la valeur qu'ils jugent — voir `app.schemas.indicators`.

Aucune de ces fonctions ne juge une mesure. La définition de « dégradée »
appartient à l'ETL du repo predict (`etl/quality.py`), qui la pose dans
`data_quality`, `null_reasons` et `imputation_method`. On compte ce qu'elle a
écrit ; en réécrire la règle ici donnerait deux définitions du même mot.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.energy import IngestionEtat, Mesure
from app.models.prediction import Modele, Prediction
from app.schemas.indicators import (
    AccuracyIndicatorOut,
    CollectorStateOut,
    IngestionIndicatorOut,
    QualityIndicatorOut,
)

# Marqueurs de panne capteur dans `null_reasons`. Il y en a DEUX parce que
# deux producteurs y écrivent, et c'est le genre de désaccord qu'une colonne
# TEXT[] sans contrainte laisse passer sans bruit :
#
# - la source nomme le capteur tombé (`consumption_sensor_failure`,
#   `electrical_sensor_failure`, `temperature_...`, `humidity_...`) ;
# - l'ETL, quand la source n'a rien déclaré, nomme la colonne restée nulle
#   suivie de `:undeclared` (etl/quality.py, DERIVED_REASON_SUFFIX).
#
# Ne reconnaître que le premier laisserait passer toute panne que la source
# n'a pas étiquetée, c'est-à-dire précisément celle que personne n'a vue.
SOURCE_SENSOR_FAILURE_MARKER = "_sensor_failure"
ETL_UNDECLARED_MARKER = ":undeclared"

# Qualification que la source et l'ETL réservent à une mesure exploitable.
QUALITY_GOOD = "good"

# Qualifications d'une mesure inexploitable ou presque.
DEGRADED_QUALITIES = ("degraded", "critical")

# Valeur de `imputation_method` disant qu'aucune reconstruction n'a eu lieu.
NO_IMPUTATION = "none"

# Valeur de `quality_source` disant que la qualification vient de l'ETL et non
# du défaut de la base.
QUALIFIED_BY_ETL = "etl"

# Séparateur du tableau de motifs aplati pour la recherche. Un caractère de
# contrôle, absent de tout motif : le prendre dans l'alphabet des motifs
# laisserait une recherche chevaucher deux entrées voisines.
REASON_SEPARATOR = "\x1f"

# Précision du regroupement horaire, argument de `date_trunc`. Nommée parce
# qu'elle EST la règle d'appariement de l'indicateur d'écart : la source
# produit à la minute, le modèle prédit à l'heure.
HOUR_PRECISION = "hour"


def _reasons_as_text() -> Any:
    """Aplatit `null_reasons` en une chaîne cherchable.

    Un `unnest` dans un EXISTS dirait la même chose plus fidèlement, au prix
    d'une sous-requête corrélée par ligne comptée. Le volume est celui d'une
    fenêtre, la concaténation suffit.
    """
    return func.array_to_string(Mesure.null_reasons, REASON_SEPARATOR)


def _has_sensor_failure() -> Any:
    """Vrai quand les motifs nomment une panne capteur, quel qu'en soit l'auteur.

    `autoescape` n'est pas décoratif : `_sensor_failure` commence par le joker
    d'un caractère de LIKE, et sans échappement le motif accepterait n'importe
    quoi devant « sensor_failure ».
    """
    reasons = _reasons_as_text()
    return reasons.contains(
        SOURCE_SENSOR_FAILURE_MARKER,
        autoescape=True,
    ) | reasons.contains(ETL_UNDECLARED_MARKER, autoescape=True)


def _is_degraded() -> Any:
    """Vrai quand la mesure est dégradée au sens de l'ETL.

    Trois symptômes, réunis par un OU parce qu'ils ne se recouvrent pas : une
    mesure peut être imputée sans être qualifiée `degraded`, et une panne
    déclarée par la source peut précéder toute requalification.
    """
    return (
        Mesure.data_quality.in_(DEGRADED_QUALITIES)
        | (Mesure.imputation_method != NO_IMPUTATION)
        | _has_sensor_failure()
    )


def _count_where(condition: Any) -> Any:
    """Compte les lignes du groupe vérifiant la condition."""
    return func.count().filter(condition)


def _as_float(value: Decimal | float | None) -> float | None:
    """Ramène un NUMERIC relu par le driver à un flottant du contrat."""
    return None if value is None else float(value)


def _ratio(part: int, whole: int) -> float:
    """Part de `part` dans `whole`, et zéro plutôt qu'une division par zéro.

    Une fenêtre vide n'a pas de part dégradée. Ce zéro-là est ambigu, et c'est
    `qualified_ratio` qui le lève : il vaut zéro aussi.
    """
    return part / whole if whole else 0.0


def window_bounds(now: datetime, window_hours: int) -> tuple[datetime, datetime]:
    """Fenêtre `[now - window_hours, now]`, bornes incluses.

    `now` est un paramètre plutôt qu'un appel à l'horloge : les trois agrégats
    et les âges publiés doivent partager le même présent, sans quoi une seule
    réponse décrirait deux instants légèrement différents.
    """
    return now - timedelta(hours=window_hours), now


async def ingestion_indicators(
    session: AsyncSession,
    site_ids: Sequence[str],
    now: datetime,
    stale_threshold_seconds: float,
) -> dict[str, IngestionIndicatorOut]:
    """Fraîcheur d'ingestion des sites demandés, indexée par site.

    Deux lectures, parce qu'elles ne répondent pas à la même question et
    qu'aucune ne remplace l'autre.

    Les mesures donnent la dernière mesure connue et le délai qu'elle a mis à
    entrer. `ingestion_etat` donne ce que le collecteur dit de lui-même — et
    c'est la seule source capable de signaler qu'il ne tourne plus, puisqu'un
    collecteur arrêté n'écrit aucune ligne et laisse `max(ts)` figé à
    l'identique d'un site qui aurait cessé d'exister.
    """
    latest = await _latest_measures(session, site_ids)
    collectors = await _collector_states(session, site_ids)
    return {
        site_id: _to_ingestion(
            latest.get(site_id),
            collectors.get(site_id),
            now,
            stale_threshold_seconds,
        )
        for site_id in site_ids
    }


async def _latest_measures(
    session: AsyncSession,
    site_ids: Sequence[str],
) -> dict[str, Any]:
    """Dernière mesure de chaque site, avec la date de son écriture.

    `DISTINCT ON` sur le site puis tri décroissant sur l'horodatage : c'est
    l'ordre de la clé primaire (site_id, ts), l'index porte donc la requête.

    `last_ingested_at` est agrégé sur la partition et non pris sur cette
    ligne : un rattrapage écrit des mesures anciennes tardivement, si bien que
    la dernière écriture n'est pas forcément celle de la dernière mesure.
    """
    rows = await session.execute(
        select(
            Mesure.site_id,
            Mesure.timestamp.label("last_measure_at"),
            Mesure.inserted_at,
            func.max(Mesure.inserted_at)
            .over(partition_by=Mesure.site_id)
            .label("last_ingested_at"),
        )
        .where(Mesure.site_id.in_(site_ids))
        .distinct(Mesure.site_id)
        .order_by(Mesure.site_id, Mesure.timestamp.desc()),
    )
    return {row.site_id: row for row in rows}


async def _collector_states(
    session: AsyncSession,
    site_ids: Sequence[str],
) -> dict[str, IngestionEtat]:
    """État publié par le collecteur, pour les sites qui en ont un."""
    rows = await session.scalars(
        select(IngestionEtat).where(IngestionEtat.site_id.in_(site_ids)),
    )
    return {state.site_id: state for state in rows}


def _to_collector(state: IngestionEtat | None) -> CollectorStateOut | None:
    """Projette l'état du collecteur, ou rien quand il n'a jamais tourné."""
    if state is None:
        return None
    return CollectorStateOut(
        last_attempt_at=state.last_attempt_at,
        last_success_at=state.last_success_at,
        last_rows=state.last_rows,
        last_data_lag_seconds=_as_float(state.last_data_lag_s),
        consecutive_failures=state.consecutive_failures,
        last_error=state.last_error,
        source=state.source,
    )


def _to_ingestion(
    latest: Any | None,
    collector: IngestionEtat | None,
    now: datetime,
    stale_threshold_seconds: float,
) -> IngestionIndicatorOut:
    """Assemble le bloc de fraîcheur d'un site à partir des deux lectures."""
    last_measure_at = None if latest is None else latest.last_measure_at
    measure_age = (
        None if last_measure_at is None else (now - last_measure_at).total_seconds()
    )
    return IngestionIndicatorOut(
        last_measure_at=last_measure_at,
        last_ingested_at=None if latest is None else latest.last_ingested_at,
        measure_age_seconds=measure_age,
        ingestion_lag_seconds=(
            None
            if latest is None
            else (latest.inserted_at - latest.last_measure_at).total_seconds()
        ),
        # Un site sans aucune mesure est en retard, pas frais. Répondre faux
        # ferait passer l'absence totale de donnée pour une donnée à jour.
        is_stale=measure_age is None or measure_age > stale_threshold_seconds,
        stale_threshold_seconds=stale_threshold_seconds,
        collector=_to_collector(collector),
    )


def _quality_query(
    site_ids: Sequence[str],
    start_time: datetime,
    end_time: datetime,
) -> Select[Any]:
    """Comptes de qualité par site sur la fenêtre, en une seule passe."""
    return (
        select(
            Mesure.site_id,
            func.count().label("total"),
            _count_where(_is_degraded()).label("degraded"),
            _count_where(Mesure.data_quality != QUALITY_GOOD).label("not_good"),
            _count_where(Mesure.imputation_method != NO_IMPUTATION).label("imputed"),
            _count_where(_has_sensor_failure()).label("sensor_failure"),
            _count_where(Mesure.quality_source == QUALIFIED_BY_ETL).label("qualified"),
        )
        .where(
            Mesure.site_id.in_(site_ids),
            Mesure.timestamp >= start_time,
            Mesure.timestamp <= end_time,
        )
        .group_by(Mesure.site_id)
    )


async def quality_indicators(
    session: AsyncSession,
    site_ids: Sequence[str],
    start_time: datetime,
    end_time: datetime,
    degraded_ratio_threshold: float,
) -> dict[str, QualityIndicatorOut]:
    """Part de mesures dégradées de chaque site, indexée par site.

    Un site sans mesure sur la fenêtre reçoit un bloc à zéro plutôt que rien :
    l'absence de mesure est une information, et la taire obligerait l'appelant
    à distinguer deux formes de réponse pour un même endpoint.
    """
    rows = {
        row.site_id: row
        for row in await session.execute(
            _quality_query(site_ids, start_time, end_time),
        )
    }
    return {
        site_id: _to_quality(
            rows.get(site_id),
            start_time,
            end_time,
            degraded_ratio_threshold,
        )
        for site_id in site_ids
    }


def _to_quality(
    counts: Any | None,
    start_time: datetime,
    end_time: datetime,
    threshold: float,
) -> QualityIndicatorOut:
    """Assemble le bloc de qualité d'un site à partir de ses comptes."""
    total = 0 if counts is None else counts.total
    degraded = 0 if counts is None else counts.degraded
    qualified = 0 if counts is None else counts.qualified
    ratio = _ratio(degraded, total)
    return QualityIndicatorOut(
        window_start=start_time,
        window_end=end_time,
        total=total,
        degraded=degraded,
        degraded_ratio=ratio,
        threshold=threshold,
        # Atteint et non dépassé : le seuil est la valeur à partir de laquelle
        # la fiabilité est compromise, pas la dernière valeur acceptable.
        exceeds_threshold=total > 0 and ratio >= threshold,
        not_good=0 if counts is None else counts.not_good,
        imputed=0 if counts is None else counts.imputed,
        sensor_failure=0 if counts is None else counts.sensor_failure,
        qualified=qualified,
        qualified_ratio=_ratio(qualified, total),
    )


def _hourly_actuals(
    site_ids: Sequence[str],
    start_time: datetime,
    end_time: datetime,
) -> Any:
    """Consommation réelle moyennée par heure et par site.

    Seule la valeur BRUTE entre dans la moyenne. Se rabattre sur
    `consumption_kw_imputed` mesurerait l'écart entre la prévision et une
    valeur que l'ETL a lui-même reconstruite, c'est-à-dire la dérive de l'ETL
    et non celle du modèle.

    L'agrégation à l'heure EST la règle d'appariement : la source produit à la
    minute, le modèle prédit à l'heure. Retenir la mesure la plus proche de
    l'horodatage cible ferait dépendre l'indicateur d'une seule minute, que
    n'importe quel à-coup rendrait aberrante.
    """
    # L'expression est construite UNE fois et réutilisée dans le SELECT comme
    # dans le GROUP BY. Deux appels séparés produiraient deux paramètres liés
    # distincts, et PostgreSQL refuserait le regroupement faute de pouvoir
    # prouver que les deux expressions sont la même.
    hour = func.date_trunc(HOUR_PRECISION, Mesure.timestamp)
    return (
        select(
            Mesure.site_id.label("site_id"),
            hour.label("hour"),
            func.avg(Mesure.consumption_kw).label("actual_kw"),
        )
        .where(
            Mesure.site_id.in_(site_ids),
            Mesure.timestamp >= start_time,
            Mesure.timestamp <= end_time,
            Mesure.consumption_kw.is_not(None),
        )
        .group_by(Mesure.site_id, hour)
        .subquery()
    )


def _has_bounds() -> Any:
    """Vrai quand la prévision annonçait un intervalle de confiance."""
    return and_(
        Prediction.lower_bound_kw.is_not(None),
        Prediction.upper_bound_kw.is_not(None),
    )


def _accuracy_query(
    site_ids: Sequence[str],
    start_time: datetime,
    end_time: datetime,
) -> Select[Any]:
    """Écart entre prévisions archivées et mesures, agrégé par site.

    La jointure est interne : une heure sans prévision et une prévision sans
    heure mesurée ne forment pas de paire, et une paire absente ne doit pas
    peser zéro dans une moyenne — elle doit ne pas exister.
    """
    actuals = _hourly_actuals(site_ids, start_time, end_time)
    error = Prediction.consumption_kw_predite - actuals.c.actual_kw
    within_bounds = case(
        (
            _has_bounds(),
            case(
                (
                    actuals.c.actual_kw.between(
                        Prediction.lower_bound_kw,
                        Prediction.upper_bound_kw,
                    ),
                    1.0,
                ),
                else_=0.0,
            ),
        ),
        # NULL et non zéro quand aucune bande n'était annoncée : `avg` ignore
        # les NULL, si bien que la part porte sur les seules prévisions qui en
        # portaient une. Un zéro les compterait comme des ratés.
        else_=None,
    )
    return (
        select(
            Prediction.site_id,
            func.count().label("paired_points"),
            func.avg(func.abs(error)).label("mae_kw"),
            func.avg(error).label("bias_kw"),
            func.avg(actuals.c.actual_kw).label("mean_actual_kw"),
            func.avg(within_bounds).label("within_bounds_ratio"),
            _count_where(_has_bounds()).label("bounded_points"),
            func.array_agg(Modele.version.distinct()).label("model_versions"),
        )
        # Jointure interne sur le registre, comme la lecture des prédictions :
        # modele_id est NOT NULL et le registre est alimenté à chaque
        # promotion.
        .join(Modele, Modele.modele_id == Prediction.modele_id)
        .join(
            actuals,
            and_(
                actuals.c.site_id == Prediction.site_id,
                actuals.c.hour
                == func.date_trunc(HOUR_PRECISION, Prediction.ts_cible),
            ),
        )
        .where(
            Prediction.site_id.in_(site_ids),
            Prediction.ts_cible >= start_time,
            Prediction.ts_cible <= end_time,
        )
        .group_by(Prediction.site_id)
    )


async def accuracy_indicators(
    session: AsyncSession,
    site_ids: Sequence[str],
    start_time: datetime,
    end_time: datetime,
    drift_mae_ratio: float,
) -> dict[str, AccuracyIndicatorOut]:
    """Écart prévision / réel de chaque site, indexé par site."""
    rows = {
        row.site_id: row
        for row in await session.execute(
            _accuracy_query(site_ids, start_time, end_time),
        )
    }
    return {
        site_id: _to_accuracy(
            rows.get(site_id),
            start_time,
            end_time,
            drift_mae_ratio,
        )
        for site_id in site_ids
    }


def _to_accuracy(
    row: Any | None,
    start_time: datetime,
    end_time: datetime,
    drift_mae_ratio: float,
) -> AccuracyIndicatorOut:
    """Assemble le bloc d'écart d'un site à partir de son agrégat."""
    mae = None if row is None else _as_float(row.mae_kw)
    mean_actual = None if row is None else _as_float(row.mean_actual_kw)
    return AccuracyIndicatorOut(
        window_start=start_time,
        window_end=end_time,
        model_versions=[] if row is None else sorted(row.model_versions),
        paired_points=0 if row is None else row.paired_points,
        mae_kw=mae,
        bias_kw=None if row is None else _as_float(row.bias_kw),
        mean_actual_kw=mean_actual,
        within_bounds_ratio=(
            None if row is None else _as_float(row.within_bounds_ratio)
        ),
        bounded_points=0 if row is None else row.bounded_points,
        drift=is_drifting(mae, mean_actual, drift_mae_ratio),
        drift_threshold_ratio=drift_mae_ratio,
    )


def is_drifting(
    mae_kw: float | None,
    mean_actual_kw: float | None,
    drift_mae_ratio: float,
) -> bool:
    """Dit si l'erreur dépasse la part tolérée de la consommation moyenne.

    Faux quand rien n'a été mesuré, et faux quand la consommation moyenne est
    nulle. Le second cas n'est pas un détail : un site à l'arrêt donnerait un
    seuil de zéro kilowatt, que la moindre erreur dépasserait, et le dashboard
    annoncerait une dérive du modèle là où il n'y a qu'un site éteint.
    """
    if mae_kw is None or not mean_actual_kw:
        return False
    return mae_kw > drift_mae_ratio * mean_actual_kw
