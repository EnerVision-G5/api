"""Fenêtre de temps des routes paginées : normalisation et validation.

Extrait des routes de lecture d'EV-11 pour être partagé avec la lecture des
prédictions, à comportement inchangé : mêmes bornes incluses, même code
d'erreur, même message. Deux routes symétriques ne doivent pas répondre
différemment à la même requête malformée.
"""

from datetime import UTC, datetime

from fastapi import HTTPException, status

INVERTED_WINDOW = "start_time doit être antérieur ou égal à end_time."


def as_utc(moment: datetime) -> datetime:
    """Ramène un horodatage de requête en UTC.

    Le contrat annonce de l'ISO 8601 UTC, mais un client peut omettre le
    décalage. Les colonnes horodatées étant des TIMESTAMPTZ, comparer un
    datetime naïf ferait échouer le driver : l'absence de décalage est donc
    interprétée comme de l'UTC plutôt que remontée en 500.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def parse_window(start_time: datetime, end_time: datetime) -> tuple[datetime, datetime]:
    """Normalise la fenêtre en UTC et refuse des bornes inversées en 422.

    La validation précède toute consultation de la base : des bornes inversées
    rendent la requête malformée quel que soit le site, elle est donc refusée
    avant le 404 du référentiel.
    """
    start_utc, end_utc = as_utc(start_time), as_utc(end_time)
    if start_utc > end_utc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=INVERTED_WINDOW,
        )
    return start_utc, end_utc
