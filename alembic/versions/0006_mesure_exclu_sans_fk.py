"""Retrait de la clé étrangère de `mesure_exclu` vers l'hypertable `mesure`.

Cette contrainte rejetait des lignes valides. Elle ne protégeait donc plus
rien : elle mettait l'ETL en échec.

Le mécanisme, parce qu'il n'est pas devinable depuis le message d'erreur.
PostgreSQL vérifie une clé étrangère par une requête préparée sur la table
référencée, et cette requête passe par le cache de plans : les cinq premières
exécutions utilisent un plan *custom*, où la constante permet à TimescaleDB
d'exclure les chunks et de trouver la ligne ; à partir de la sixième,
PostgreSQL envisage un plan *générique*, où le paramètre n'est plus connu à la
planification. L'exclusion de chunks ne s'y fait plus, la ligne n'est pas
trouvée, et la contrainte rejette une insertion parfaitement valide.

D'où un seuil qui n'a rien de métier : jusqu'à cinq exclusions par instruction
tout passe, à partir de six tout échoue. Une journée de panne réseau sur un
site en produit 1440.

    ERROR:  insert or update on table "mesure_exclu" violates foreign key
            constraint "mesure_exclu_site_id_ts_fkey"
    DETAIL:  Key (site_id, ts)=(SITE002, 2026-08-04 00:05:00+00) is not
             present in table "mesure".

alors que la ligne EST présente, et qu'un `INSERT` d'une seule ligne portant
cette même clé réussit. Vérifié sur TimescaleDB 2.17.2 / PostgreSQL 16, et
levé en forçant `plan_cache_mode = force_custom_plan` — ce qui confirme le
diagnostic, mais ne peut pas servir de correctif : le réglage vaut pour toute
la base, API comprise, et priverait chaque requête de son plan générique pour
compenser une contrainte inutilisable.

Ce qui garantit l'intégrité à la place. L'ETL est le seul à écrire cette
table, et il écrit `mesure` AVANT les exclusions, dans la même passe et dans
cet ordre — voir `etl/load.py`, où l'ordre est nommé comme non négociable
précisément à cause de cette clé. L'invariant est donc tenu par le code qui
produit les lignes, et non plus par la base qui les reçoit.

Ce que l'on perd, et qu'il faut assumer. Une exclusion orpheline ne serait
plus rejetée à l'écriture. La lecture, elle, ne s'en trouve pas fragilisée :
`etl/extract.py` joint `mesure_exclu` en LEFT JOIN depuis `mesure`, donc une
ligne orpheline n'y apparaît jamais.

L'index UNIQUE (site_id, ts) reste : c'est lui qui porte le `ON CONFLICT` du
chargement, et le retirer casserait le rejeu d'une fenêtre.

Revision ID: 0006_mesure_exclu_sans_fk
Revises: 0005_capteur_panne
Create Date: 2026-09-07

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_mesure_exclu_sans_fk"
down_revision: str | None = "0005_capteur_panne"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT_NAME = "mesure_exclu_site_id_ts_fkey"


def upgrade() -> None:
    # IF EXISTS : la contrainte est absente d'une base créée après ce jour par
    # un autre chemin, et la migration doit rester rejouable.
    op.execute(
        f"ALTER TABLE mesure_exclu DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME}"
    )


def downgrade() -> None:
    # Repose la contrainte telle que 0001 la posait. Le retour arrière rétablit
    # donc aussi la panne : c'est le prix d'un downgrade fidèle, et la raison
    # pour laquelle il ne faut le jouer que pour revenir à une version
    # antérieure de l'API, jamais pour « remettre l'intégrité ».
    op.execute(
        f"ALTER TABLE mesure_exclu ADD CONSTRAINT {CONSTRAINT_NAME}"
        " FOREIGN KEY (site_id, ts) REFERENCES mesure (site_id, ts)"
    )
