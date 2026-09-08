"""DTO de gestion des utilisateurs (EV-55).

Source de vérité du contrat. Toute modification exige une PR sur
enervision/docs/contracts et la relecture des trois consommateurs.

Distinct de `UserOut`, qui décrit le porteur du jeton et ne publie que ce dont
l'appelant a besoin sur lui-même — identifiant et rôle. La gestion demande
davantage : qui a créé le compte, quand, s'il s'est déjà connecté, et s'il peut
se connecter du tout. `UserOut` reste inchangé, il est câblé sur chaque
endpoint protégé.

Trois choses ne sortent jamais d'ici, et ce n'est pas un oubli :

- le **hachage** du mot de passe, ni aucune de ses métadonnées. Un
  administrateur n'a rien à en faire, et le publier n'offrirait qu'une prise à
  qui obtiendrait une réponse ;
- le mot de passe **en clair**, évidemment, y compris juste après une
  création : celui qui le pose le connaît déjà ;
- `oauth_provider`. Seuls les comptes locaux sont gérables par ces routes, la
  valeur serait donc constante — et une identité fédérée ne se crée pas par
  mot de passe.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Rôles applicatifs, repris de la contrainte CHECK de la table app_user : les
# deux doivent bouger ensemble, et le Literal fait échouer la requête avant la
# base plutôt qu'après.
UserRole = Literal["reader", "writer"]

# Identifiant de connexion. Le motif refuse les espaces et la ponctuation qui
# se confondent à la lecture d'un journal : un compte « dev reader » et un
# compte « dev  reader » seraient deux comptes indiscernables à l'œil.
Username = Annotated[
    str,
    StringConstraints(min_length=3, max_length=255, pattern=r"^[A-Za-z0-9._-]+$"),
]

# Adresse de courriel, validée par un motif plutôt que par `EmailStr`.
#
# `EmailStr` exigerait email-validator, absent du lock. L'ajouter pour vérifier
# la forme d'une adresse dans un MVP coûterait une dépendance de plus dans
# l'image, et l'en-tête de `requirements.txt` prévient qu'on ne touche pas à ce
# fichier à la légère. Le motif refuse ce qui n'est manifestement pas une
# adresse ; il ne prétend pas qu'elle est délivrable — aucune validation
# syntaxique ne peut le dire.
EmailAddress = Annotated[
    str,
    StringConstraints(
        min_length=5,
        max_length=255,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$",
    ),
]

# Longueur minimale du mot de passe. Douze caractères plutôt que huit : c'est
# le seuil au-delà duquel une attaque par dictionnaire cesse d'être triviale,
# et aucune règle de composition n'est imposée — elles poussent à des mots de
# passe prévisibles plutôt que longs.
MIN_PASSWORD_LENGTH = 12
Password = Annotated[str, StringConstraints(min_length=MIN_PASSWORD_LENGTH, max_length=128)]


class UserDetailOut(BaseModel):
    """Utilisateur tel que la gestion le publie."""

    model_config = ConfigDict(from_attributes=True)

    user_id: int = Field(description="Identifiant technique du compte.")
    username: str = Field(description="Identifiant de connexion.")
    email: str = Field(description="Adresse de courriel du compte.")
    display_name: str | None = Field(
        default=None,
        description="Nom affiché, nul si le compte n'en porte pas.",
    )
    role: UserRole = Field(
        description="Rôle applicatif : reader en lecture seule, writer en écriture.",
    )
    can_sign_in: bool = Field(
        description=(
            "Vrai si le compte porte un mot de passe local. Faux pour une"
            " identité fédérée, qui ne peut pas passer par le flux mot de"
            " passe."
        ),
    )
    created_at: datetime = Field(description="Création du compte, ISO 8601 UTC.")
    last_login_at: datetime | None = Field(
        default=None,
        description=(
            "Dernière connexion aboutie, ISO 8601 UTC. Nulle si le compte ne"
            " s'est jamais connecté — ce qui distingue un compte neuf d'un"
            " compte abandonné."
        ),
    )


class UserCreateIn(BaseModel):
    """Compte à créer."""

    model_config = ConfigDict(extra="forbid")

    username: Username = Field(
        description="Identifiant de connexion, unique parmi les comptes locaux.",
    )
    email: EmailAddress = Field(description="Adresse de courriel du compte.")
    display_name: str | None = Field(
        default=None,
        max_length=150,
        description="Nom affiché, facultatif.",
    )
    role: UserRole = Field(
        default="reader",
        description=(
            "Rôle applicatif. reader par défaut : un compte reçoit le droit"
            " d'écrire parce qu'on le lui donne, jamais par omission."
        ),
    )
    password: Password = Field(
        description=(
            f"Mot de passe, {MIN_PASSWORD_LENGTH} caractères au minimum."
            " Haché en argon2id avant écriture, jamais conservé en clair."
        ),
    )


class UserUpdateIn(BaseModel):
    """Modification d'un compte.

    Tous les champs sont facultatifs, et seuls ceux présents sont appliqués :
    une requête ne portant que le rôle ne doit pas effacer le nom affiché.
    `extra="forbid"` refuse un champ inconnu plutôt que de l'ignorer en
    silence — une faute de frappe sur « role » se solderait sinon par une
    modification qui n'a pas lieu et une réponse 200.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailAddress | None = Field(default=None, description="Nouvelle adresse de courriel.")
    display_name: str | None = Field(
        default=None,
        max_length=150,
        description="Nouveau nom affiché.",
    )
    role: UserRole | None = Field(default=None, description="Nouveau rôle applicatif.")
    password: Password | None = Field(
        default=None,
        description=(
            "Nouveau mot de passe. Le fournir réinitialise l'accès du compte ;"
            " l'omettre laisse le mot de passe en place."
        ),
    )
