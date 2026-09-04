"""Point d'entrée Alembic.

L'URL vient de app.core.config, jamais d'alembic.ini : le mot de passe n'a
rien à faire dans un fichier versionné.

Le dialecte déployé est asyncpg (DATABASE_URL de l'API). Un `engine_from_config`
classique ne sait pas l'ouvrir — il faut le moteur asyncio de SQLAlchemy. Le
chemin synchrone reste disponible pour une URL psycopg, dont se servent les
jobs batch du repo predict.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, engine_from_config, make_url, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models  # noqa: F401  (charge les modèles pour peupler Base.metadata)
from app.core.config import settings
from app.db.base import Base

config = context.config

# set_main_option passe par ConfigParser, qui interprète `%` : un mot de passe
# contenant un caractère échappé en %XX ferait échouer la lecture de l'URL.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    if make_url(settings.database_url).get_dialect().is_async:
        asyncio.run(run_async_migrations())
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
