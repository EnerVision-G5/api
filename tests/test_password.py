"""Tests du hachage des mots de passe (EV-12, argon2id)."""

import pytest

from app.password import hash_password, needs_rehash, verify_password
from tests.conftest import TEST_PASSWORD, weak_hasher

ARGON2ID_PREFIX = "$argon2id$"


def test_hash_is_argon2id() -> None:
    """Le hachage annonce argon2id, pas une autre variante d'argon2."""
    assert hash_password(TEST_PASSWORD).startswith(ARGON2ID_PREFIX)


def test_hash_differs_at_each_call() -> None:
    """Deux hachages du même mot de passe diffèrent : le sel est aléatoire.

    Sans cela, une base volée révélerait quels comptes partagent un mot de
    passe, et un dictionnaire précalculé suffirait à les casser en bloc.
    """
    first = hash_password(TEST_PASSWORD)
    second = hash_password(TEST_PASSWORD)

    assert first != second
    # Les deux restent valides : c'est le sel qui change, pas le mot de passe.
    assert verify_password(TEST_PASSWORD, first)
    assert verify_password(TEST_PASSWORD, second)


def test_verify_accepts_the_right_password() -> None:
    assert verify_password(TEST_PASSWORD, hash_password(TEST_PASSWORD)) is True


def test_verify_refuses_a_wrong_password() -> None:
    assert verify_password("pas-le-bon", hash_password(TEST_PASSWORD)) is False


@pytest.mark.parametrize(
    ("case", "hashed"),
    [
        ("hachage tronque", hash_password("peu-importe")[:40]),
        ("chaine vide", ""),
        ("texte quelconque", "pas-un-hachage-du-tout"),
        # Format d'un hachage bcrypt : ce que la base contenait avant le
        # passage à argon2id. Il doit être refusé, pas provoquer une erreur.
        ("hachage bcrypt", "$2a$12$0xSN4w3vF/4NxbxUMKEww.HO4q.1dApnYw5Vk8Sg5V25LJ9thbe8K"),
        ("separateurs seuls", "$$$$"),
    ],
)
def test_verify_refuses_a_broken_hash_without_raising(case: str, hashed: str) -> None:
    """Un hachage illisible se répond False, sans exception ni indice sur la cause."""
    assert verify_password(TEST_PASSWORD, hashed) is False, case


@pytest.mark.parametrize("hashed", [None, ""])
def test_verify_refuses_an_absent_hash(hashed: str | None) -> None:
    """Une colonne password_hash à NULL ne doit pas faire tomber l'appelant.

    C'est le cas d'une identité fédérée : elle existe en base sans mot de
    passe local, et ne peut donc pas se connecter par ce flux.
    """
    assert verify_password(TEST_PASSWORD, hashed) is False


@pytest.mark.parametrize("hashed", [None, ""])
def test_needs_rehash_is_false_on_an_absent_hash(hashed: str | None) -> None:
    assert needs_rehash(hashed) is False


def test_needs_rehash_is_false_on_a_current_hash() -> None:
    assert needs_rehash(hash_password(TEST_PASSWORD)) is False


def test_needs_rehash_is_true_on_weaker_parameters() -> None:
    """Un hachage produit avec des paramètres plus faibles est à remplacer."""
    weak = weak_hasher.hash(TEST_PASSWORD)

    assert weak.startswith(ARGON2ID_PREFIX)
    assert needs_rehash(weak) is True
    # Il reste vérifiable en attendant son remplacement : personne n'est
    # enfermé dehors par un durcissement des paramètres.
    assert verify_password(TEST_PASSWORD, weak) is True


def test_needs_rehash_is_false_on_a_broken_hash() -> None:
    """Rien à tirer d'un hachage illisible, donc rien à rehacher.

    Le cas ne se présente pas en pratique : la vérification l'aurait déjà
    refusé avant qu'on en arrive là.
    """
    assert needs_rehash("pas-un-hachage-du-tout") is False
