"""Hachage des mots de passe : argon2id, et rien d'autre.

Seul module du projet autorisé à manipuler un hachage. Tout le reste passe par
hash_password, verify_password et needs_rehash : concentrer le sujet ici évite
qu'un second mécanisme s'installe ailleurs, et rend un changement de
paramètres ou d'algorithme relisible d'un coup d'œil.

Pourquoi argon2id, décision d'équipe :

- c'est la recommandation OWASP de premier choix pour un nouveau projet ;
- il résiste au matériel dédié en coûtant de la mémoire, là où bcrypt ne coûte
  que du temps ;
- argon2-cffi est activement maintenu, tandis que passlib ne l'est plus et se
  brouille avec les versions récentes de bcrypt.

Les paramètres sont ceux d'argon2-cffi par défaut, volontairement : ils sont
au-dessus des minimums OWASP (m=19 MiB, t=2, p=1) et suivent l'évolution de la
bibliothèque sans intervention de notre part.

    argon2id, t=3, m=64 MiB, p=4, hash 32 octets, sel 16 octets

Ne pas les abaisser sans mesurer, et jamais en dessous des minimums ci-dessus.
"""

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

# InvalidHashError dérive de ValueError et non d'Argon2Error : les deux
# familles doivent être citées, sans quoi un hachage illisible remonterait
# jusqu'à l'appelant au lieu d'être un simple refus.
_VERIFICATION_FAILURES = (Argon2Error, InvalidHashError, TypeError)

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """Hache un mot de passe en argon2id.

    Le sel est tiré au hasard à chaque appel : deux hachages du même mot de
    passe diffèrent, et une base volée ne révèle pas qui partage un mot de
    passe avec qui.
    """
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str | None) -> bool:
    """Dit si le mot de passe correspond au hachage. Ne lève jamais.

    Mot de passe faux, hachage tronqué, hachage d'un autre algorithme, hachage
    absent : tout se répond False, sans distinguer la cause. L'appelant n'a
    aucun usage légitime du détail, et le lui donner reviendrait à renseigner
    un attaquant sur l'état de la base.

    L'absence de hachage est écartée d'entrée plutôt que laissée à un
    try/except : la colonne password_hash est nullable, une identité fédérée
    n'en a pas, et ce refus est donc un cas prévu et non un accident dont il
    faudrait rattraper l'exception.
    """
    if not hashed:
        return False
    try:
        return _hasher.verify(hashed, plain)
    except _VERIFICATION_FAILURES:
        return False


def needs_rehash(hashed: str | None) -> bool:
    """Dit si un hachage a été produit avec des paramètres dépassés.

    À appeler après une vérification réussie : c'est le seul moment où le mot
    de passe en clair est disponible pour produire le remplaçant. Un hachage
    absent ou illisible répond False, faute de pouvoir en tirer quoi que ce
    soit ; le cas ne devrait pas se présenter puisque la vérification l'aurait
    déjà refusé.
    """
    if not hashed:
        return False
    try:
        return _hasher.check_needs_rehash(hashed)
    except _VERIFICATION_FAILURES:
        return False
