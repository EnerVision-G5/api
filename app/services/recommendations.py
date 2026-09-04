"""Moteur de recommandations d'action (EV-32).

Trois règles, et une seule source d'entrée : ce que la chaîne a déjà écrit.
Aucune collecte, aucun appel au service d'inférence, aucune table — les
recommandations se recalculent à chaque lecture depuis les prévisions
courantes. Les archiver figerait un conseil que la prévision suivante
contredirait.

Les règles ne jugent pas la donnée, elles la lisent :

- `predicted_peak` cherche la plage où la prévision se tient au-dessus d'une
  fraction de son propre maximum. C'est une pointe relative, et c'est
  volontaire : un site à 40 % de sa capacité a lui aussi des heures creuses
  vers lesquelles décaler ses usages ;
- `capacity_overrun` compare la prévision à la puissance souscrite. C'est la
  seule règle absolue des trois, parce que le dépassement l'est aussi : au
  delà, le fournisseur facture ou le disjoncteur ouvre ;
- `sensor_failure` regarde ce sur quoi la prévision s'appuie. Une prévision
  calculée sur des heures majoritairement imputées n'est pas fausse, elle est
  mal fondée, et c'est une information que le chiffre seul ne porte pas.

Ces trois-là peuvent tomber ensemble sur un même site, et rien ne les
hiérarchise entre elles : elles s'adressent à trois interlocuteurs différents.
Le tri final se fait sur la gravité, pas sur la nature.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Row, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.energy import Mesure, Site
from app.models.observation import CapteurEtat
from app.models.prediction import Modele, Prediction
from app.schemas.recommendation import (
    RecommendationOut,
    RecommendationSeverity,
    RecommendationsOut,
)

# Profondeur examinée par défaut. Vingt-quatre heures : c'est l'horizon que le
# job de prédiction rafraîchit, en demander plus lirait des heures qu'aucun
# modèle n'a encore produites.
DEFAULT_HORIZON_HOURS = 24
MAX_HORIZON_HOURS = 168

# Une pointe commence là où la prévision atteint cette fraction de son propre
# maximum sur la fenêtre. 0.9 isole le sommet ; descendre trop bas nommerait
# « pointe » la moitié de la journée et ne dirait plus rien.
PEAK_RATIO = 0.9

# En deçà de deux points au-dessus du seuil, ce n'est pas une plage mais un
# accident d'échantillonnage : rien à décaler.
MIN_PEAK_POINTS = 2

# Part de la fenêtre récente reconstruite par l'ETL au delà de laquelle la
# prévision est tenue pour mal fondée. La moitié : en dessous, le modèle a vu
# assez de vrai pour que son résultat garde un sens.
IMPUTED_RATIO_THRESHOLD = 0.5

# Profondeur sur laquelle cette part est mesurée. Les décalages du modèle
# portent sur les heures récentes : remonter plus loin diluerait une panne en
# cours dans un historique sain.
IMPUTATION_LOOKBACK_HOURS = 24

# Marge de dépassement au delà de laquelle le délestage devient critique
# plutôt que sévère : un dixième de la puissance souscrite.
CRITICAL_OVERRUN_RATIO = 0.1

NO_PREDICTION = (
    "Aucune prévision disponible sur la fenêtre : aucune action ne peut être"
    " proposée. Vérifier le service d'inférence et le job de rafraîchissement."
)
NOTHING_TO_REPORT = (
    "Aucune action à proposer : la prévision reste sous la puissance souscrite"
    " et ne dessine aucune pointe marquée."
)


def _clock() -> datetime:
    """Présent de référence, isolé pour que les tests le remplacent."""
    return datetime.now(UTC)


async def _predictions(
    session: AsyncSession,
    site_id: str,
    start: datetime,
    end: datetime,
) -> Sequence[Row]:
    """Prévisions du site sur la fenêtre, avec la version qui les a produites.

    Triées par instant cible : les règles lisent une série, pas un ensemble.
    """
    statement = (
        select(
            Prediction.ts_cible,
            Prediction.consumption_kw_predite,
            Modele.version.label("model_version"),
        )
        .join(Modele, Modele.modele_id == Prediction.modele_id)
        .where(
            Prediction.site_id == site_id,
            Prediction.ts_cible >= start,
            Prediction.ts_cible <= end,
        )
        .order_by(Prediction.ts_cible)
    )
    return (await session.execute(statement)).all()


async def _imputed_ratio(
    session: AsyncSession,
    site_id: str,
    since: datetime,
) -> tuple[float, int]:
    """Part des mesures récentes reconstruites par l'ETL, et leur nombre.

    La définition d'« imputée » n'est pas écrite ici : c'est l'ETL qui pose
    `imputation_method`, et en réécrire la règle donnerait deux définitions du
    même mot.
    """
    statement = select(
        func.count().label("total"),
        func.count()
        .filter(Mesure.imputation_method != "none")
        .label("imputed"),
    ).where(Mesure.site_id == site_id, Mesure.timestamp >= since)
    row = (await session.execute(statement)).one()
    total = int(row.total or 0)
    if total == 0:
        return 0.0, 0
    return int(row.imputed or 0) / total, total


async def _failing_sensors(session: AsyncSession, site_id: str) -> list[str]:
    """Capteurs que la source déclare en panne à l'instant présent."""
    statement = (
        select(CapteurEtat.capteur)
        .where(CapteurEtat.site_id == site_id, CapteurEtat.statut == "failing")
        .order_by(CapteurEtat.capteur)
    )
    return list(await session.scalars(statement))


def _peak_window(
    points: Sequence[Row],
) -> tuple[datetime, datetime, float] | None:
    """Retourne la plage de pointe et sa valeur maximale, ou rien.

    La plage est la plus longue suite contiguë de points au-dessus du seuil.
    Contiguë et non l'ensemble des points : dire « décaler hors de 08h–20h »
    parce que deux sommets distants dépassent ne serait pas un conseil.
    """
    values = [float(point.consumption_kw_predite) for point in points]
    if not values:
        return None
    peak = max(values)
    threshold = peak * PEAK_RATIO

    best: tuple[int, int] | None = None
    start: int | None = None
    for index, value in enumerate(values + [float("-inf")]):
        if value >= threshold:
            start = index if start is None else start
            continue
        if start is not None:
            if best is None or (index - start) > (best[1] - best[0]):
                best = (start, index)
            start = None
    if best is None or (best[1] - best[0]) < MIN_PEAK_POINTS:
        return None
    return points[best[0]].ts_cible, points[best[1] - 1].ts_cible, peak


def _hhmm(moment: datetime) -> str:
    """Heure au format HH:MM, tel que la formulation métier l'attend."""
    return moment.strftime("%H:%M")


def _peak_recommendation(points: Sequence[Row]) -> RecommendationOut | None:
    """« Décaler les usages non critiques hors de la fenêtre HH:MM–HH:MM »."""
    window = _peak_window(points)
    if window is None:
        return None
    start, end, peak = window
    return RecommendationOut(
        type="predicted_peak",
        severity="medium",
        message=(
            "Décaler les usages non critiques hors de la fenêtre"
            f" {_hhmm(start)}–{_hhmm(end)}"
        ),
        window_start=start,
        window_end=end,
        value_kw=round(peak, 2),
    )


def _overrun_recommendation(
    points: Sequence[Row],
    capacity_kw: Decimal,
) -> RecommendationOut | None:
    """« Délester X kW à HH:MM, ou réviser la puissance souscrite »."""
    capacity = float(capacity_kw)
    over = [
        point
        for point in points
        if float(point.consumption_kw_predite) > capacity
    ]
    if not over:
        return None
    worst = max(over, key=lambda point: float(point.consumption_kw_predite))
    excess = float(worst.consumption_kw_predite) - capacity
    severity: RecommendationSeverity = (
        "critical" if excess > capacity * CRITICAL_OVERRUN_RATIO else "high"
    )
    return RecommendationOut(
        type="capacity_overrun",
        severity=severity,
        message=(
            f"Délester {excess:.0f} kW à {_hhmm(worst.ts_cible)}, ou réviser la"
            " puissance souscrite"
        ),
        at=worst.ts_cible,
        value_kw=round(excess, 2),
    )


def _sensor_recommendation(
    imputed_ratio: float,
    measured: int,
    failing: Sequence[str],
) -> RecommendationOut | None:
    """« Intervention capteur : […] fiabilité dégradée ».

    Deux déclencheurs, et il faut les deux. Un capteur que la source déclare
    en panne est un fait présent ; une part d'imputation élevée est la trace
    d'une panne que la source n'a peut-être jamais déclarée. Ne garder que le
    premier laisserait passer précisément les pannes que personne n'a vues.
    """
    if not failing and (measured == 0 or imputed_ratio < IMPUTED_RATIO_THRESHOLD):
        return None
    return RecommendationOut(
        type="sensor_failure",
        severity="high" if failing else "medium",
        message=(
            "Intervention capteur : la prévision s'appuie sur des données"
            " imputées, fiabilité dégradée"
        ),
    )


# Ordre de tri des actions servies. La gravité et non la nature : les trois
# règles s'adressent à trois interlocuteurs, aucune ne prime par principe.
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


async def recommendations_for(
    session: AsyncSession,
    site: Site,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    now: datetime | None = None,
) -> RecommendationsOut:
    """Calcule les actions proposées pour un site, sans rien écrire."""
    generated_at = now or _clock()
    end = generated_at + timedelta(hours=horizon_hours)
    points = await _predictions(session, site.site_id, generated_at, end)

    imputed_ratio, measured = await _imputed_ratio(
        session,
        site.site_id,
        generated_at - timedelta(hours=IMPUTATION_LOOKBACK_HOURS),
    )
    failing = await _failing_sensors(session, site.site_id)
    sensor = _sensor_recommendation(imputed_ratio, measured, failing)

    if not points:
        # La panne capteur reste servie : elle ne dépend pas de la prévision,
        # et c'est même souvent elle qui explique son absence.
        items = [sensor] if sensor is not None else []
        return RecommendationsOut(
            site_id=site.site_id,
            generated_at=generated_at,
            horizon_hours=horizon_hours,
            model_version=None,
            items=items,
            detail=NO_PREDICTION,
        )

    proposed = [
        _peak_recommendation(points),
        _overrun_recommendation(points, site.capacity_kw),
        sensor,
    ]
    items = sorted(
        (item for item in proposed if item is not None),
        key=lambda item: SEVERITY_ORDER[item.severity],
    )
    return RecommendationsOut(
        site_id=site.site_id,
        generated_at=generated_at,
        horizon_hours=horizon_hours,
        # Une seule version servie : les points d'une fenêtre viennent du même
        # rafraîchissement. Plusieurs signalerait une promotion en cours, et
        # c'est la plus récente qui décrit ce que le site reçoit maintenant.
        model_version=points[-1].model_version,
        items=items,
        detail=None if items else NOTHING_TO_REPORT,
    )
