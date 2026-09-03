"""DTO des indicateurs de confiance dans la donnée (EV-18).

Trois indicateurs, et ils ne répondent pas à la même question. La fraîcheur
d'ingestion dit si la donnée arrive encore. La part de mesures dégradées dit
ce qu'elle vaut. L'écart entre prévision et consommation réelle dit si le
modèle qui s'appuie dessus tient encore la route.

Ils sont réunis dans une seule réponse parce qu'ils sont lus ensemble : c'est
la confiance dans l'écran qu'ils qualifient, et un exploitant qui voit une
courbe veut savoir d'un coup d'œil si elle est à jour, propre et prédite
correctement. Trois appels séparés donneraient trois états de chargement pour
une seule question.

Les trois blocs sont toujours présents, mais leurs champs sont nullables et se
suffisent à eux-mêmes. Une prévision absente ne doit pas effacer la fraîcheur
d'ingestion : c'est la même discipline que les deux erreurs indépendantes du
hook `useSiteSeries` du dashboard. Un bloc nul aurait obligé le front à
distinguer « pas de bloc » de « bloc vide », alors qu'un compte à zéro le dit
déjà.

Chaque seuil est publié à côté de la valeur qu'il juge. Sans cela, le
dashboard devrait le redéclarer, et deux vérités divergeraient sans que rien
ne le signale — c'est exactement ce qui s'est produit sur le seuil de
fraîcheur, à 120 s côté front contre 180 s côté collecteur.

Source de vérité du contrat. Toute modification exige une PR sur
enervision/docs/contracts et la relecture des trois consommateurs.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Points d'entrée du collecteur, tels que le CHECK de `ingestion_etat` les
# admet. Un rattrapage lancé à la main ne doit pas se faire passer pour une
# collecte vivante.
CollectorSource = Literal["poller", "backfill"]


class CollectorStateOut(BaseModel):
    """Ce que le collecteur dit de lui-même pour un site.

    Nul quand aucune ligne n'existe : le collecteur n'a jamais tourné sur ce
    site, ou la migration `06_ingestion_etat.sql` n'est pas appliquée. Les
    deux se disent « je ne sais pas », jamais « tout va bien ».
    """

    model_config = ConfigDict(from_attributes=True)

    last_attempt_at: datetime = Field(
        description=(
            "Dernier essai de collecte, abouti ou non, ISO 8601 UTC. Comparé à"
            " last_success_at : égaux, la collecte va bien ; écartés, elle"
            " tourne et échoue."
        ),
    )
    last_success_at: datetime | None = Field(
        description=(
            "Dernier essai abouti, ISO 8601 UTC. Nul tant qu'aucun n'a réussi."
        ),
    )
    last_rows: int = Field(
        description=(
            "Lignes soumises par le dernier essai abouti. Zéro est une réponse"
            " valable : la source a répondu, elle n'avait rien de nouveau."
        ),
    )
    last_data_lag_seconds: float | None = Field(
        description=(
            "Âge de la mesure servie par la source au dernier essai abouti,"
            " mesuré par le collecteur. Distinct de ingestion_lag_seconds, qui"
            " mesure le retard de l'écriture."
        ),
    )
    consecutive_failures: int = Field(
        description=(
            "Échecs consécutifs depuis le dernier succès. Remis à zéro par un"
            " succès : il distingue l'à-coup de la panne installée."
        ),
    )
    last_error: str | None = Field(
        description=(
            "Cause du dernier échec. Conservée après un succès : savoir de quoi"
            " un site relève a une valeur."
        ),
    )
    source: CollectorSource = Field(
        description=(
            "Point d'entrée ayant écrit cet état : poller pour la collecte"
            " continue, backfill pour un rattrapage manuel."
        ),
    )


class IngestionIndicatorOut(BaseModel):
    """Fraîcheur de la dernière ingestion d'un site.

    Deux horloges, et il faut les deux. L'âge de la mesure dit si la donnée
    décrit encore le présent. Le retard d'écriture dit si la chaîne suit. Un
    collecteur arrêté et un collecteur qui rattrape du retard donnent la même
    valeur sur la première et deux valeurs très différentes sur la seconde.
    """

    model_config = ConfigDict(from_attributes=True)

    last_measure_at: datetime | None = Field(
        description=(
            "Horodatage de la mesure la plus récente du site, ISO 8601 UTC."
            " Nul quand le site n'a aucune mesure."
        ),
    )
    last_ingested_at: datetime | None = Field(
        description=(
            "Horodatage de la dernière écriture en base, ISO 8601 UTC. Peut"
            " être bien postérieur à last_measure_at après un rattrapage."
        ),
    )
    measure_age_seconds: float | None = Field(
        description=(
            "Âge de la mesure la plus récente au moment de la réponse, en"
            " secondes."
        ),
    )
    ingestion_lag_seconds: float | None = Field(
        description=(
            "Délai entre la mesure la plus récente et son écriture en base, en"
            " secondes. Mesure ce que la chaîne a mis à la charger, pas ce que"
            " la source a mis à la servir."
        ),
    )
    is_stale: bool = Field(
        description=(
            "Vrai quand la mesure la plus récente dépasse le seuil, ou quand le"
            " site n'a aucune mesure. Un âge inconnu n'est jamais tenu pour"
            " frais."
        ),
    )
    stale_threshold_seconds: float = Field(
        description=(
            "Seuil au-delà duquel l'ingestion est tenue pour en retard, servi"
            " ici pour que le dashboard ne le redéclare pas."
        ),
    )
    collector: CollectorStateOut | None = Field(
        description=(
            "État que le collecteur publie pour ce site. Nul quand il n'a jamais"
            " tourné dessus : c'est la seule chose que les mesures ne peuvent"
            " pas dire, un collecteur arrêté n'écrivant aucune ligne."
        ),
    )


class QualityIndicatorOut(BaseModel):
    """Part de mesures dégradées d'un site sur la fenêtre.

    « Dégradée » est la définition de l'ETL, pas une seconde définition écrite
    ici : une mesure est dégradée quand la source ou l'ETL l'a qualifiée
    `degraded` ou `critical`, quand sa valeur a dû être reconstruite, ou quand
    ses motifs d'absence nomment une panne capteur. L'API compte, elle ne juge
    pas.

    Une mesure écartée par un analyste (`mesure_exclu`) n'entre PAS dans ce
    compte : l'exclusion est un jugement humain sur une valeur aberrante, la
    dégradation est un fait capteur. Les confondre rendrait le chiffre
    indéfendable en recette.
    """

    model_config = ConfigDict(from_attributes=True)

    window_start: datetime = Field(
        description="Borne inférieure incluse de la fenêtre, ISO 8601 UTC.",
    )
    window_end: datetime = Field(
        description="Borne supérieure incluse de la fenêtre, ISO 8601 UTC.",
    )
    total: int = Field(description="Mesures du site sur la fenêtre.")
    degraded: int = Field(
        description="Mesures dégradées au sens de l'ETL, voir la description du modèle.",
    )
    degraded_ratio: float = Field(
        description=(
            "Part de mesures dégradées, entre 0 et 1. Vaut 0 sur une fenêtre"
            " vide, que qualified_ratio permet alors de distinguer d'une"
            " fenêtre saine."
        ),
    )
    threshold: float = Field(
        description=(
            "Part au-delà de laquelle la fiabilité du site est tenue pour"
            " compromise, servie ici pour que le dashboard ne la redéclare pas."
        ),
    )
    exceeds_threshold: bool = Field(
        description="Vrai quand degraded_ratio atteint le seuil.",
    )
    not_good: int = Field(
        description="Mesures dont data_quality n'est pas good.",
    )
    imputed: int = Field(
        description="Mesures dont la valeur a été reconstruite par l'ETL.",
    )
    sensor_failure: int = Field(
        description=(
            "Mesures dont les motifs d'absence nomment une panne capteur, dans"
            " le vocabulaire de la source (*_sensor_failure) comme dans celui"
            " de l'ETL (*:undeclared)."
        ),
    )
    qualified: int = Field(
        description=(
            "Mesures dont la qualification a été posée par l'ETL et non laissée"
            " au défaut de la base."
        ),
    )
    qualified_ratio: float = Field(
        description=(
            "Part de la fenêtre réellement qualifiée, entre 0 et 1. Le chiffre"
            " le plus important du bloc : data_quality vaut good par défaut, et"
            " une fenêtre non encore traitée par l'ETL affiche donc 0 % de"
            " dégradation sans que cela veuille dire qu'elle est saine."
        ),
    )


class AccuracyIndicatorOut(BaseModel):
    """Écart entre les prévisions servies et la consommation réellement mesurée.

    Détecteur de dérive côté exploitation. La comparaison porte sur les
    prévisions ARCHIVÉES, c'est-à-dire celles qui ont réellement été servies,
    récursives sur leur horizon. Ce n'est pas le même nombre que la
    surveillance de dérive du repo predict, qui mesure l'erreur à un pas sur
    les décalages réels : celle-là juge le modèle, celle-ci juge ce que le
    client a reçu, et elle sera toujours la moins flatteuse des deux.

    Règle d'appariement, publiée parce qu'elle décide du chiffre : les mesures
    sont moyennées par heure, les prévisions rattachées à l'heure de leur
    horodatage cible. Une heure sans aucune mesure de puissance BRUTE ne forme
    pas de paire — comparer une prévision à une valeur imputée mesurerait la
    dérive de l'ETL, pas celle du modèle.

    Pas de MAPE : elle explose quand la consommation approche zéro, et une
    seule heure creuse suffirait à rendre l'indicateur illisible. Le biais
    signé la remplace utilement, en donnant la direction de l'erreur.
    """

    model_config = ConfigDict(from_attributes=True)

    window_start: datetime = Field(
        description="Borne inférieure incluse de la fenêtre, ISO 8601 UTC.",
    )
    window_end: datetime = Field(
        description="Borne supérieure incluse de la fenêtre, ISO 8601 UTC.",
    )
    model_versions: list[str] = Field(
        description=(
            "Versions de modèle ayant produit les prévisions comparées, triées."
            " Plus d'une entrée signale une fenêtre à cheval sur une promotion :"
            " l'écart mêle alors deux modèles."
        ),
    )
    paired_points: int = Field(
        description=(
            "Heures pour lesquelles une prévision et une mesure brute existent"
            " toutes deux. Zéro rend les autres champs nuls, et c'est le cas"
            " courant tant qu'aucune prévision n'est archivée."
        ),
    )
    mae_kw: float | None = Field(
        description=(
            "Erreur absolue moyenne en kilowatts. La MAE plutôt que la RMSE :"
            " elle s'exprime dans l'unité du compteur, ce qui se discute avec"
            " un exploitant."
        ),
    )
    bias_kw: float | None = Field(
        description=(
            "Moyenne de prédit moins réel, en kilowatts. Signée à dessein :"
            " c'est elle qui dit si le modèle surestime ou sous-estime, ce"
            " qu'une erreur absolue ne peut pas dire."
        ),
    )
    mean_actual_kw: float | None = Field(
        description=(
            "Consommation réelle moyenne sur les heures appariées, en"
            " kilowatts. Sert de référence à drift : une erreur ne se juge pas"
            " en valeur absolue."
        ),
    )
    within_bounds_ratio: float | None = Field(
        description=(
            "Part des mesures tombant dans l'intervalle de confiance annoncé,"
            " entre 0 et 1. Nulle quand aucune prévision appariée ne porte de"
            " bornes."
        ),
    )
    bounded_points: int = Field(
        description=(
            "Heures appariées dont la prévision portait un intervalle."
            " within_bounds_ratio porte sur celles-là seulement."
        ),
    )
    drift: bool = Field(
        description=(
            "Vrai quand mae_kw dépasse drift_threshold_ratio fois"
            " mean_actual_kw. Faux quand aucune paire n'existe : rien n'a été"
            " mesuré, ce n'est pas une absence de dérive mais elle ne doit pas"
            " être annoncée."
        ),
    )
    drift_threshold_ratio: float = Field(
        description=(
            "Rapport toléré entre l'erreur et la consommation moyenne, servi ici"
            " pour que le dashboard ne le redéclare pas."
        ),
    )


class SiteIndicatorsOut(BaseModel):
    """Les trois indicateurs de confiance d'un site, à un instant donné."""

    model_config = ConfigDict(from_attributes=True)

    site_id: str = Field(description="Identifiant du site décrit.")
    generated_at: datetime = Field(
        description=(
            "Horodatage du calcul, ISO 8601 UTC. C'est lui qui sert de présent"
            " aux âges publiés."
        ),
    )
    window_hours: int = Field(
        description="Profondeur de la fenêtre sur laquelle les parts sont calculées.",
    )
    ingestion: IngestionIndicatorOut = Field(
        description="Fraîcheur de la dernière ingestion.",
    )
    quality: QualityIndicatorOut = Field(
        description="Part de mesures dégradées sur la fenêtre.",
    )
    accuracy: AccuracyIndicatorOut = Field(
        description="Écart entre prévisions servies et consommation réelle.",
    )
