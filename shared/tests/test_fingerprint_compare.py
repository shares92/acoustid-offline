"""``compare2`` als reine Funktion: Eigenschaften und Randfaelle.

Die bit-genaue Gegenprobe gegen die Original-Extension steht in
`test_fingerprint_extension.py` und braucht einen Container. Hier stehen die
Zusicherungen, die **ohne** Referenz gelten muessen — damit ein Fehler auch
dann auffaellt, wenn der Container gerade nicht laeuft.
"""

from __future__ import annotations

import random
import struct

import pytest

from shared.fingerprint import MAX_OFFSET, compare2

_RNG = random.Random(20260726)


def _vector(length: int) -> list[int]:
    return [_RNG.randrange(0, 2**32) for _ in range(length)]


def _is_float32(value: float) -> bool:
    """Ist der Wert exakt in einfacher Genauigkeit darstellbar?"""
    return struct.unpack("<f", struct.pack("<f", value))[0] == value


# --- Grundzusicherungen -----------------------------------------------------


@pytest.mark.parametrize("length", [80, 120, 500, 948])
def test_identical_vectors_score_one(length: int) -> None:
    vector = _vector(length)
    assert compare2(vector, vector, MAX_OFFSET) == 1.0


def test_unrelated_vectors_score_far_below_the_cutoff() -> None:
    """Der Lookup-Cutoff liegt bei 0,4 — Zufall darf ihn nie erreichen."""
    for _ in range(20):
        score = compare2(_vector(948), _vector(948), MAX_OFFSET)
        assert 0.0 <= score < 0.4


def test_empty_input_is_zero_and_never_raises() -> None:
    assert compare2([], [], MAX_OFFSET) == 0.0
    assert compare2([], _vector(100), MAX_OFFSET) == 0.0
    assert compare2(_vector(100), [], MAX_OFFSET) == 0.0


def test_result_is_always_a_single_precision_value() -> None:
    """Das Original liefert ``float4``; alles andere waere ein Rundungsfehler."""
    for _ in range(20):
        a = _vector(400)
        b = [value ^ (1 << _RNG.randrange(0, 32)) for value in a]
        assert _is_float32(compare2(a, b, MAX_OFFSET))


def test_score_stays_between_zero_and_one() -> None:
    for _ in range(30):
        a = _vector(_RNG.randrange(80, 600))
        b = _vector(_RNG.randrange(80, 600))
        assert 0.0 <= compare2(a, b, MAX_OFFSET) <= 1.0


# --- Verhalten bei Stoerungen ----------------------------------------------


def test_more_bit_errors_mean_a_lower_score() -> None:
    vector = _vector(600)
    previous = 1.0
    for flips in (0, 1, 2, 4, 8):
        noisy = list(vector)
        for position in range(0, len(noisy), max(1, len(noisy) // 60)):
            for _ in range(flips):
                noisy[position] ^= 1 << _RNG.randrange(0, 32)
        score = compare2(vector, noisy, MAX_OFFSET)
        assert score <= previous
        previous = score


def test_a_shifted_recording_still_matches() -> None:
    """Ein Mitschnitt, der spaeter beginnt, muss trotzdem gefunden werden."""
    full = _vector(900)
    shifted = full[60:]
    assert compare2(full, shifted, MAX_OFFSET) > 0.4


def test_shift_beyond_the_offset_limit_is_not_matched() -> None:
    """Jenseits von ``max_offset`` richtet die Funktion nicht mehr aus."""
    full = _vector(900)
    far = full[300:]
    assert compare2(full, far, MAX_OFFSET) < 0.4
    # Ohne Grenze (0 = unbegrenzt) findet sie dieselbe Stelle wieder.
    assert compare2(full, far, 0) > 0.4


def test_a_short_common_section_lowers_the_score() -> None:
    """Nur ein Teilstueck gemeinsam -> deutlich weniger als 1.0."""
    a = _vector(800)
    b = a[:200] + _vector(600)
    score = compare2(a, b, MAX_OFFSET)
    assert 0.0 < score < 0.6


# --- Argumente --------------------------------------------------------------


def test_signed_and_unsigned_are_the_same_input() -> None:
    """Vollvektoren aus Postgres sind signed, die Anfrage ist unsigned."""
    unsigned = _vector(300)
    signed = [value - 2**32 if value & 0x80000000 else value for value in unsigned]
    assert compare2(signed, unsigned, MAX_OFFSET) == 1.0
    assert compare2(signed, signed, MAX_OFFSET) == 1.0


def test_arguments_are_not_modified() -> None:
    a = _vector(300)
    b = _vector(300)
    copies = (list(a), list(b))
    compare2(a, b, MAX_OFFSET)
    assert (a, b) == copies


def test_repeated_calls_are_deterministic() -> None:
    a = _vector(500)
    b = a[30:] + _vector(30)
    assert compare2(a, b, MAX_OFFSET) == compare2(a, b, MAX_OFFSET)


def test_short_vectors_do_not_raise() -> None:
    """Sehr kurze Vektoren (das Original wuerde hier ueber den Puffer laufen)."""
    for length in (1, 2, 3, 10, 79):
        assert 0.0 <= compare2(_vector(length), _vector(length), MAX_OFFSET) <= 1.0


@pytest.mark.parametrize("max_offset", [0, 1, 80, 1000])
def test_every_offset_limit_is_accepted(max_offset: int) -> None:
    a = _vector(400)
    assert compare2(a, a, max_offset) == 1.0
