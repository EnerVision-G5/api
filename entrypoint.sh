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
# sur un schéma incomplet. La base indisponible au démarrage tombe dans le
# même cas, et c'est la politique de redémarrage du conteneur
# (restart: unless-stopped) qui fait office de réessai, avec son propre délai
# croissant.
#
# NB : predict-cron partage cette image mais écrase `entrypoint` dans son
# compose. Il ne migre donc pas — c'est voulu, une seule chose doit faire
# évoluer le schéma.
set -e

alembic upgrade head

exec "$@"
