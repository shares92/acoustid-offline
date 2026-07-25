"""Gemeinsame Enums (ARCHITECTURE §5.2, §6, §9)."""

import pytest

from shared.models import AuthMode, StackState, SubmissionStatus, SubmitMode


def test_auth_modes_match_the_documentation() -> None:
    assert [mode.value for mode in AuthMode] == ["none", "apikey"]


def test_submit_modes_match_the_documentation() -> None:
    assert [mode.value for mode in SubmitMode] == ["off", "local", "local+upstream"]


def test_submission_status_matches_the_state_machine() -> None:
    assert [status.value for status in SubmissionStatus] == [
        "new",
        "indexed",
        "forwarded",
        "forward_failed",
    ]


def test_stack_states_match_the_documentation() -> None:
    assert [state.value for state in StackState] == [
        "sleeping",
        "starting",
        "ready",
        "stopping",
        "error",
    ]


def test_stack_states_have_the_german_labels_from_section_9() -> None:
    assert [state.display_name for state in StackState] == [
        "schlafend",
        "startet",
        "bereit",
        "stoppt",
        "fehler",
    ]


@pytest.mark.parametrize(
    "enum_class", [AuthMode, StackState, SubmissionStatus, SubmitMode], ids=lambda cls: cls.__name__
)
def test_every_member_has_a_display_name(enum_class: type) -> None:
    for member in enum_class:
        assert member.display_name


@pytest.mark.parametrize(
    "enum_class", [AuthMode, StackState, SubmissionStatus, SubmitMode], ids=lambda cls: cls.__name__
)
def test_members_behave_like_their_string_value(enum_class: type) -> None:
    """StrEnum: der Wert geht unveraendert in YAML, JSON und SQL."""
    for member in enum_class:
        assert member == member.value
        assert f"{member}" == member.value
        assert enum_class(member.value) is member


def test_unknown_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="basic"):
        AuthMode("basic")
