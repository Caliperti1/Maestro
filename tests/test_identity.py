from app.core.identity import is_maestro_user_reference


def test_maestro_user_identity_matches_unambiguous_references() -> None:
    assert is_maestro_user_reference(name="Chris Aliperti")
    assert is_maestro_user_reference(name="Chris A.")
    assert is_maestro_user_reference(email="chris.aliperti@praxis-defense.com")
    assert is_maestro_user_reference(name="Chris Aliperti <chris.aliperti@praxis-defense.com>")
    assert is_maestro_user_reference(name="me")


def test_maestro_user_identity_does_not_claim_ambiguous_or_other_people() -> None:
    assert not is_maestro_user_reference(name="Chris")
    assert not is_maestro_user_reference(name="Chris F")
    assert not is_maestro_user_reference(name="Chris Flournoy")

