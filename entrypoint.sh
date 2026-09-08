#!/bin/sh
# Point d'entrée de l'image de production.
#
# L'API porte son schéma : elle applique ses migrations avant d'accepter du
# trafic, plutôt que d'attendre qu'un déploiement pense à le faire pour elle.
# Un conteneur qui démarre sur une base sans tables répondrait 500 sur chaque
# lecture, et rien dans la chaîne de déploiement ne le dirait.
#
# `alembic upgrade head` lit `alembic_version` : si la base est déjà à la
# bonne révision, il ne fait rien et l'API démarre. Ce n'est donc pas une
# réexécution à chaque redémarrage, c'est une vérification.
#
# `set -e` : une migration qui échoue arrête le conteneur au lieu de servir
# sur un schéma incomplet.
#
# Une base qui DÉMARRE ENCORE est le seul cas qui ne relève pas de cette
# règle, et on l'attend ici plutôt que de sortir. Au redémarrage de l'hôte, le
# démon Docker relance tous les conteneurs ensemble sans lire les `depends_on`
# du compose — ceux-ci n'ordonnent que `compose up` — si bien que l'API gagne
# la course sur TimescaleDB le temps qu'il rejoue son WAL. Sortir laissait la
# politique de redémarrage rattraper le coup, mais au prix d'une trace Python
# complète dans le journal pour ce qui n'est qu'un ordre de démarrage.
#
# NB : predict-cron partage cette image mais écrase `entrypoint` dans son
# compose. Il ne migre donc pas — c'est voulu, une seule chose doit faire
# évoluer le schéma.
set -e

# Fenêtre d'attente : 2 s x 30, soit une minute. La reprise mesurée sur un
# redémarrage de poste est de l'ordre de dix secondes ; au-delà d'une minute,
# ce n'est plus un démarrage, et il vaut mieux rendre la trace que boucler en
# silence.
DB_WAIT_DELAY_S=2
DB_WAIT_MAX_ATTEMPTS=30

attempt=1
while :; do
  # Sortie capturée : elle n'est rendue qu'une fois qu'on sait la qualifier.
  if migration_log=$(alembic upgrade head 2>&1); then
    if [ -n "$migration_log" ]; then
      printf '%s\n' "$migration_log" >&2
    fi
    break
  fi

  # Ce que Postgres et asyncpg répondent tant que la base n'accepte pas de
  # connexion. Rien de tout cela ne peut venir d'une migration fautive : une
  # révision en défaut échoue sur son SQL, pas sur l'ouverture du socket.
  case "$migration_log" in
  *"the database system is starting up"* | *CannotConnectNowError*)
    db_state="elle démarre encore"
    ;;
  *"the database system is shutting down"*)
    db_state="elle s'arrête"
    ;;
  *"the database system is in recovery mode"*)
    db_state="elle se répare"
    ;;
  *ConnectionRefusedError* | *"Connect call failed"*)
    # Le conteneur de la base n'a pas encore de socket ouvert. Même course,
    # quelques secondes plus tôt.
    db_state="elle n'est pas encore joignable"
    ;;
  *"Name or service not known"* | *"Temporary failure in name resolution"*)
    db_state="son nom ne se résout pas encore"
    ;;
  *)
    # Migration en échec pour une autre raison : là, la trace EST le message,
    # et le conteneur doit s'arrêter.
    printf '%s\n' "$migration_log" >&2
    exit 1
    ;;
  esac

  if [ "$attempt" -ge "$DB_WAIT_MAX_ATTEMPTS" ]; then
    echo "ERROR: base indisponible apres $DB_WAIT_MAX_ATTEMPTS tentatives," \
         "trace ci-dessous." >&2
    printf '%s\n' "$migration_log" >&2
    exit 1
  fi

  echo "INFO: base en attente : $db_state" \
       "(tentative $attempt/$DB_WAIT_MAX_ATTEMPTS)." >&2
  attempt=$((attempt + 1))
  sleep "$DB_WAIT_DELAY_S"
done

exec "$@"
