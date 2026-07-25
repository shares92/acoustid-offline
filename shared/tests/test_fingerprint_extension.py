"""Bit-Verifikation gegen die Original-Extension pg_acoustid (ARCHITECTURE §5.3).

Das Rescoring der Matching-Pipeline rechnet in Python nach, was bei
acoustid.org eine C-Extension in der Datenbank tut (DECISIONS 2026-07-25
„Rescoring per Python-Nachbau mit CI-Bit-Verifikation"). Dieser Nachbau darf
nicht „ungefaehr" stimmen: ein um ein ULP verschobener Score verschiebt den
Cutoff, und die Query-Extraktion entscheidet ueberhaupt erst darueber, ob ein
Fingerprint gefunden wird. Deshalb laeuft die Original-Extension als
**Test-Container** mit und liefert die Wahrheit, gegen die hier verglichen
wird — Wert fuer Wert, ohne Toleranz.

Der Container wird nie ausgeliefert (pg_acoustid hat keine Lizenzdatei); der
Stack faehrt das unveraenderte offizielle Postgres-Image.

Vorbereitung::

    docker build -t acoustid-offline-pg-acoustid:test tests/pg_acoustid
    docker run -d --rm --name pg-acoustid -p 127.0.0.1:5443:5432 \\
        -e POSTGRES_PASSWORD=test acoustid-offline-pg-acoustid:test
    ACOUSTID_EXTENSION_DSN=postgresql://postgres:test@127.0.0.1:5443/postgres \\
        uv run pytest -m extension --integration=require

Geprueft wird mit **echten** Fingerprint-Vektoren aus den Tages-Deltas
(sofern die Fixtures da sind) UND mit Zufallsvektoren zu einem festen Seed.
Die Zufallsfaelle sind bewusst so gebaut, dass sie die unangenehmen Zweige
treffen: wenig Vielfalt (dann daempft das Original den Score mit ``pow``),
Hash-Praefix 16383 (dort hat das Original eine Speicher-Eigenheit, siehe
:func:`shared.fingerprint.compare._count_unique`) und eingestreute
Silence-Hashes (sie verschieben das Extraktionsfenster).

Die Ergebnisse kommen als ``float8`` zurueck: die Erweiterung von ``float4``
auf ``float8`` ist verlustfrei, und so wird nicht ueber die Textdarstellung
verglichen.
"""

from __future__ import annotations

import gzip
import json
import os
import random
from collections.abc import Iterator, Sequence
from pathlib import Path

import psycopg
import pytest

from shared.fingerprint import compare2
from shared.fpindex import SILENCE_HASH, extract_query

pytestmark = [pytest.mark.integration, pytest.mark.extension]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests/fixtures/acoustid-dumps/2026-07-22-fingerprint-update.jsonl.gz"

#: Fest verdrahtete Fenstergroesse der C-Funktion (``ACOUSTID_QUERY_LENGTH``);
#: nur mit diesem Wert ist ein Vergleich moeglich.
QUERY_HASHES = 120

#: ``TRACK_MAX_OFFSET`` — der Wert, mit dem der Lookup rechnet.
MAX_OFFSET = 80

_RNG = random.Random(20260725)


# --- Zugang ----------------------------------------------------------------


@pytest.fixture(scope="module")
def extension() -> Iterator[psycopg.Connection]:
    """Verbindung zur Postgres mit der Original-Extension."""
    dsn = os.environ.get("ACOUSTID_EXTENSION_DSN", "").strip()
    if not dsn:  # pragma: no cover - die Auswahl macht die conftest.py
        pytest.skip("ACOUSTID_EXTENSION_DSN ist nicht gesetzt")
    with psycopg.connect(dsn, autocommit=True) as connection:
        yield connection


def _signed(values: Sequence[int]) -> list[int]:
    """Beliebige Schreibweise -> int4, wie die Spalte sie haelt.

    Erst auf 32 Bit beschneiden, dann das Vorzeichen setzen: die Vektoren aus
    den Deltas sind bereits signed, die aus dem Dekodierer unsigned.
    """
    result: list[int] = []
    for value in values:
        masked = value & 0xFFFFFFFF
        result.append(masked - 0x100000000 if masked & 0x80000000 else masked)
    return result


def _unsigned(values: Sequence[int]) -> list[int]:
    return [value & 0xFFFFFFFF for value in values]


def _reference_query(connection: psycopg.Connection, vector: Sequence[int]) -> list[int]:
    row = connection.execute(
        "SELECT acoustid_extract_query(%s::int4[])", (_signed(vector),)
    ).fetchone()
    assert row is not None
    return _unsigned(row[0])


def _reference_score(
    connection: psycopg.Connection,
    a: Sequence[int],
    b: Sequence[int],
    max_offset: int,
) -> float:
    row = connection.execute(
        "SELECT acoustid_compare2(%s::int4[], %s::int4[], %s)::float8",
        (_signed(a), _signed(b), max_offset),
    ).fetchone()
    assert row is not None
    return row[0]


# --- Vektoren --------------------------------------------------------------


def _random_vector(
    length: int, *, alphabet_size: int = 0, top_share: float = 0.0, silence_share: float = 0.0
) -> list[int]:
    """Zufallsvektor mit steuerbarer Vielfalt und Sonderwerten.

    ``alphabet_size`` klein heisst „viele Wiederholungen" — genau dann
    daempft das Original den Score. ``top_share`` streut Hashes mit dem
    Praefix 16383 ein (die Speicher-Eigenheit), ``silence_share`` den
    Silence-Hash (er verschiebt das Extraktionsfenster).
    """
    alphabet = [_RNG.randrange(0, 2**32) for _ in range(alphabet_size)] if alphabet_size else None
    values: list[int] = []
    for _ in range(length):
        roll = _RNG.random()
        if roll < top_share:
            values.append(0xFFFC0000 | _RNG.randrange(0, 1 << 18))
        elif roll < top_share + silence_share:
            values.append(SILENCE_HASH)
        elif alphabet is not None:
            values.append(_RNG.choice(alphabet))
        else:
            values.append(_RNG.randrange(0, 2**32))
    return values


def _random_vectors() -> list[list[int]]:
    """Fester Satz Zufallsvektoren — Seed fix, damit Fehler reproduzierbar sind."""
    vectors = [_random_vector(length) for length in (1, 2, 5, 79, 80, 81, 119, 120, 121, 948)]
    vectors.append([SILENCE_HASH] * 50)
    for alphabet_size in (3, 12, 40):
        vectors.append(_random_vector(400, alphabet_size=alphabet_size))
        vectors.append(_random_vector(900, alphabet_size=alphabet_size, top_share=0.15))
    for silence_share in (0.05, 0.4):
        vectors.append(_random_vector(600, silence_share=silence_share))
        vectors.append(_random_vector(200, alphabet_size=20, silence_share=silence_share))
    return vectors


@pytest.fixture(scope="module")
def random_vectors() -> list[list[int]]:
    return _random_vectors()


@pytest.fixture(scope="module")
def real_vectors() -> list[list[int]]:
    """Echte Vollvektoren aus einem Tages-Delta (falls die Fixture da ist)."""
    if not FIXTURE.exists():
        pytest.skip(
            f"Fixture fehlt: {FIXTURE.relative_to(REPO_ROOT)} — "
            "'uv run python tests/fixtures/fetch_fixtures.py' holt sie nach"
        )
    vectors: list[list[int]] = []
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            vector = row.get("fingerprint")
            if vector:
                vectors.append(vector)
            if len(vectors) == 60:
                break
    return vectors


# --- extract_query ---------------------------------------------------------


def test_extract_query_matches_the_extension_on_random_vectors(
    extension: psycopg.Connection, random_vectors: list[list[int]]
) -> None:
    for vector in random_vectors:
        assert extract_query(vector, max_hashes=QUERY_HASHES) == _reference_query(
            extension, vector
        ), f"Abweichung bei einem Zufallsvektor der Laenge {len(vector)}"


def test_extract_query_matches_the_extension_on_real_vectors(
    extension: psycopg.Connection, real_vectors: list[list[int]]
) -> None:
    for vector in real_vectors:
        assert extract_query(vector, max_hashes=QUERY_HASHES) == _reference_query(
            extension, vector
        ), f"Abweichung bei einem echten Vektor der Laenge {len(vector)}"


def test_extract_query_window_starts_in_the_original_vector(
    extension: psycopg.Connection,
) -> None:
    """Der Startoffset zaehlt Stille NICHT heraus — der haeufigste Irrtum.

    Wer die Stille erst entfernt und dann 80 Positionen vorrueckt, landet an
    einer anderen Stelle. Dieser Fall belegt den Unterschied ausdruecklich.
    """
    payload = [0x3000 + index * 16 for index in range(200)]
    with_silence: list[int] = []
    for value in payload:
        with_silence.extend([SILENCE_HASH, value])
    ours = extract_query(with_silence, max_hashes=QUERY_HASHES)
    assert ours == _reference_query(extension, with_silence)
    # 400 Positionen, davon 200 Stille -> Offset 80 zeigt auf with_silence[80],
    # also auf payload[40] (und nicht auf payload[80]).
    assert ours[0] == payload[40]


# --- compare2 --------------------------------------------------------------


def _pairs(vectors: list[list[int]]) -> list[tuple[list[int], list[int]]]:
    """Paare, die alle interessanten Faelle abdecken."""
    pairs: list[tuple[list[int], list[int]]] = []
    for position, vector in enumerate(vectors):
        pairs.append((vector, vector))  # identisch
        pairs.append((vector, vectors[(position + 1) % len(vectors)]))  # fremd
        if len(vector) > 100:
            pairs.append((vector, vector[40:] + _random_vector(40)))  # verschoben
            pairs.append(
                (vector, [value ^ (1 << _RNG.randrange(0, 32)) for value in vector])
            )  # verrauscht
            pairs.append((vector, vector[: len(vector) // 2]))  # Teilstueck
    return pairs


def test_compare2_matches_the_extension_on_random_vectors(
    extension: psycopg.Connection, random_vectors: list[list[int]]
) -> None:
    checked = 0
    for a, b in _pairs(random_vectors):
        for max_offset in (0, MAX_OFFSET):
            assert compare2(a, b, max_offset) == _reference_score(extension, a, b, max_offset), (
                f"Abweichung bei Laengen {len(a)}/{len(b)}, max_offset {max_offset}"
            )
            checked += 1
    assert checked >= 150, "der Zufallssatz soll die Zweige wirklich abdecken"


def test_compare2_matches_the_extension_on_real_vectors(
    extension: psycopg.Connection, real_vectors: list[list[int]]
) -> None:
    for a, b in _pairs(real_vectors[:20]):
        for max_offset in (0, MAX_OFFSET):
            assert compare2(a, b, max_offset) == _reference_score(extension, a, b, max_offset)


def test_compare2_matches_around_the_lookup_cutoff(extension: psycopg.Connection) -> None:
    """Genau am Cutoff 0,4 entscheidet die letzte Stelle ueber Treffer/Nichttreffer."""
    base = _random_vector(900)
    for cut in range(300, 800, 50):
        mixed = base[:cut] + _random_vector(900 - cut)
        ours = compare2(base, mixed, MAX_OFFSET)
        assert ours == _reference_score(extension, base, mixed, MAX_OFFSET)


def test_compare2_is_not_symmetric_in_the_same_way_as_the_original(
    extension: psycopg.Connection,
) -> None:
    """Die Argumentreihenfolge zaehlt — und muss in beide Richtungen stimmen."""
    a = _random_vector(700, alphabet_size=30, top_share=0.1)
    b = _random_vector(500)
    assert compare2(a, b, MAX_OFFSET) == _reference_score(extension, a, b, MAX_OFFSET)
    assert compare2(b, a, MAX_OFFSET) == _reference_score(extension, b, a, MAX_OFFSET)


def test_compare2_handles_empty_arrays_like_the_sql_wrapper(
    extension: psycopg.Connection,
) -> None:
    """Leere Arrays faengt im Original die SQL-Huelle ab (``ARRISVOID``)."""
    vector = _random_vector(300)
    assert compare2([], vector, MAX_OFFSET) == _reference_score(extension, [], vector, MAX_OFFSET)
    assert compare2(vector, [], MAX_OFFSET) == _reference_score(extension, vector, [], MAX_OFFSET)
