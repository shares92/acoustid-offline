"""Query-Extraktion: Maske, Dedup, Silence, Offset, signed -> unsigned.

Diese Funktion entscheidet, was ueberhaupt im Index landet — sie wird in
Phase 9 zusaetzlich bit-genau gegen die Original-C-Extension geprueft. Hier
stehen die Eigenschaften, die sie unabhaengig von jeder Referenz erfuellen
muss, jeweils an Randfaellen UND an Zufallsdaten (fester Seed, damit ein
Fehlschlag reproduzierbar ist).
"""

from __future__ import annotations

import random

import pytest

from shared.fpindex import HASH_MASK, QUERY_START_OFFSET, SILENCE_HASH, extract_query

UINT32 = 0xFFFFFFFF

#: Zufallsvektoren wie aus den Deltas: signed int32, Laengen um 948.
_RNG = random.Random(20260725)


def _vector(length: int, *, silence: int = 0) -> list[int]:
    """Signed-int32-Vektor mit optional eingestreuten Silence-Hashes."""
    values = [_RNG.randrange(-(2**31), 2**31) for _ in range(length)]
    for position in _RNG.sample(range(length), k=min(silence, length)):
        values[position] = SILENCE_HASH
    return values


def _vectors() -> list[list[int]]:
    return [
        [],
        [SILENCE_HASH],
        [SILENCE_HASH] * 50,
        _vector(1),
        _vector(79),
        _vector(80),
        _vector(81),
        _vector(119),
        _vector(120),
        _vector(121),
        _vector(199),
        _vector(200),
        _vector(201),
        _vector(948),
        _vector(948, silence=40),
        _vector(2000, silence=300),
    ]


# --- Eigenschaften ---------------------------------------------------------


@pytest.mark.parametrize("max_hashes", [1, 2, 80, 120, 500])
def test_all_outputs_are_masked_unsigned_and_unique(max_hashes: int) -> None:
    """Maske, Wertebereich, Dedup und Laengengrenze gelten immer."""
    for vector in _vectors():
        query = extract_query(vector, max_hashes=max_hashes)
        assert len(query) <= max_hashes
        assert len(set(query)) == len(query), "Duplikate wuerden den Score verfaelschen"
        for value in query:
            assert 0 <= value <= UINT32, "der Index kennt nur u32"
            assert value & ~HASH_MASK == 0, "die unteren 4 Bit muessen 0 sein"


def test_silence_hash_never_survives() -> None:
    """Der Silence-Hash faellt vor der Maskierung raus, nicht danach."""
    masked_silence = SILENCE_HASH & HASH_MASK
    for vector in _vectors():
        query = extract_query(vector, max_hashes=120)
        assert SILENCE_HASH not in query
    # Ein Vektor NUR aus Stille ergibt eine leere Query — und nicht etwa
    # einen einzigen maskierten Silence-Wert.
    assert extract_query([SILENCE_HASH] * 300, max_hashes=120) == []
    # Ein Hash, der zufaellig auf denselben maskierten Wert faellt, bleibt
    # dagegen erhalten: gefiltert wird der exakte Wert, nicht das Band.
    neighbour = SILENCE_HASH + 1
    assert neighbour != SILENCE_HASH
    assert extract_query([neighbour], max_hashes=120) == [masked_silence]


def test_output_order_follows_the_input() -> None:
    """Die Reihenfolge bleibt die des Vektors (der Server sortiert selbst)."""
    vector = [0x10, 0x20, 0x30, 0x40]
    assert extract_query(vector, max_hashes=4) == [0x10, 0x20, 0x30, 0x40]


# --- Startoffset -----------------------------------------------------------


def test_offset_is_zero_while_the_vector_is_short() -> None:
    """Solange nicht mehr als `max_hashes` da sind, beginnt es bei 0."""
    vector = list(range(0x1000, 0x1000 + 120 * 16, 16))  # 120 verschiedene Hashes
    assert extract_query(vector, max_hashes=120) == vector
    assert extract_query(vector[:50], max_hashes=120) == vector[:50]


def test_offset_grows_until_it_reaches_eighty() -> None:
    """`min(cleansize - max_hashes, 80)`: das Fenster wandert mit."""
    step = 16
    base = 0x1000
    vector = [base + i * step for i in range(300)]

    # 130 Hashes, max 120 -> Offset 10.
    assert extract_query(vector[:130], max_hashes=120)[0] == vector[10]
    # 200 Hashes, max 120 -> Offset 80 (die Deckelung greift).
    assert extract_query(vector[:200], max_hashes=120)[0] == vector[80]
    # 300 Hashes, max 120 -> weiterhin Offset 80.
    assert extract_query(vector, max_hashes=120)[0] == vector[QUERY_START_OFFSET]


def test_offset_boundary_is_exactly_at_max_hashes_plus_eighty() -> None:
    """Der Grenzfall `cleansize == max_hashes + 80` liegt genau auf 80."""
    step = 16
    vector = [0x2000 + i * step for i in range(200)]
    assert extract_query(vector, max_hashes=120)[0] == vector[80]
    assert extract_query(vector[:199], max_hashes=120)[0] == vector[79]


def test_silence_shortens_the_window_but_not_the_index() -> None:
    """Stille zaehlt beim Offset nicht mit — das Fenster liegt trotzdem im Rohvektor.

    Das ist die Eigenheit des C-Originals: ``cleansize`` zaehlt die Hashes
    ohne Stille, der daraus errechnete Offset zeigt aber in den
    **unbereinigten** Vektor. Wer die Stille erst entfernt und dann zaehlt,
    landet an einer anderen Stelle — hier bei ``payload[80]`` statt
    ``payload[40]``. Die bit-genaue Gegenprobe steht in
    `test_fingerprint_extension.py`.
    """
    step = 16
    payload = [0x3000 + i * step for i in range(200)]
    with_silence = []
    for value in payload:
        with_silence.extend([SILENCE_HASH, value])
    # 400 Positionen, davon 200 Stille: cleansize 200 -> Offset min(200-120, 80) = 80.
    # with_silence[80] ist Stille, with_silence[81] ist payload[40].
    assert extract_query(with_silence, max_hashes=120)[0] == payload[40]


# --- signed -> unsigned ----------------------------------------------------


def test_negative_int32_values_become_unsigned() -> None:
    """Vollvektoren aus Postgres sind signed; der Index kennt nur u32."""
    assert extract_query([-16], max_hashes=4) == [0xFFFFFFF0]
    assert extract_query([-1], max_hashes=4) == [0xFFFFFFF0]
    assert extract_query([-(2**31)], max_hashes=4) == [0x80000000]


def test_signed_and_unsigned_notation_are_the_same_input() -> None:
    """-1 und 0xFFFFFFFF sind derselbe Hash und gelten als Duplikat."""
    assert extract_query([-1, UINT32], max_hashes=8) == [0xFFFFFFF0]


def test_silence_hash_is_matched_on_the_unsigned_value() -> None:
    """Auch in Zweierkomplement-Schreibweise wird die Stille erkannt."""
    assert extract_query([SILENCE_HASH - 2**32], max_hashes=8) == []


def test_real_world_vector_stays_in_range() -> None:
    """Ein Vektor mit den Extremwerten aus den echten Deltas."""
    vector = [-2076182035, 1946262943, -1, 0, 2**31 - 1]
    query = extract_query(vector, max_hashes=120)
    assert all(0 <= value <= UINT32 for value in query)


# --- Fehlbedienung ---------------------------------------------------------


@pytest.mark.parametrize("max_hashes", [0, -1, -120])
def test_max_hashes_below_one_is_rejected(max_hashes: int) -> None:
    with pytest.raises(ValueError, match="max_hashes"):
        extract_query([1, 2, 3], max_hashes=max_hashes)


def test_accepts_any_iterable() -> None:
    """Generatoren und Tupel sind zulaessig, nicht nur Listen."""
    assert extract_query(iter([0x10, 0x20]), max_hashes=4) == [0x10, 0x20]
    assert extract_query((0x10, 0x20), max_hashes=4) == [0x10, 0x20]


def test_input_is_not_modified() -> None:
    vector = [-16, SILENCE_HASH, 0x20]
    copy = list(vector)
    extract_query(vector, max_hashes=4)
    assert vector == copy


# --- Kopplung Indexieren <-> Suchen ---------------------------------------


def test_same_vector_and_max_hashes_give_the_same_query() -> None:
    """Deterministisch — sonst faende die Suche das eigene Dokument nicht."""
    vector = _vector(948, silence=20)
    assert extract_query(vector, max_hashes=120) == extract_query(vector, max_hashes=120)


def test_a_different_max_hashes_gives_a_different_query() -> None:
    """Belegt, warum eine Aenderung den Index-Neuaufbau erzwingt."""
    vector = _vector(948)
    assert extract_query(vector, max_hashes=80) != extract_query(vector, max_hashes=120)
