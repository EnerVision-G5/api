"""Modèle ORM de la table app_user, utilisée par l'authentification (EV-12).

Reflet en lecture du schéma figé du repo infra : 01_schema.sql pour le socle,
04_app_user_auth.sql pour password_hash et les rôles reader / writer. Ce module
ne fait pas évoluer le schéma, toute divergence avec ces fichiers est un bug
à corriger ici.

Un compte local porte oauth_provider = 'local' et oauth_subject = son username
(ADR-009, le flux OAuth2 mot de passe a été retenu contre la fédération
externe). La contrainte UNIQUE (oauth_provider, oauth_subject) du schéma v1.0
sert de clé de connexion.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Valeur d'oauth_provider identifiant un compte authentifié par mot de passe
# contre cette table, par opposition à une identité fédérée.
LOCAL_PROVIDER = "local"


class AppUser(Base):
    """Utilisateur de l'API."""

    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint(
            "oauth_provider",
            "oauth_subject",
            name="app_user_oauth_provider_oauth_subject_key",
        ),
        CheckConstraint(
            "role IN ('reader', 'writer')",
            name="app_user_role_check",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    oauth_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # Identifiant de connexion, publié tel quel dans UserOut.username et dans
    # le claim sub du JWT.
    oauth_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(150))
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'reader'"),
    )
    # Nullable : une identité fédérée n'a pas de mot de passe local et ne peut
    # alors pas se connecter par mot de passe.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
