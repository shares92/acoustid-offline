"""Query-Extraktion: Fingerprint-Vollvektor -> Index-Query (ARCHITECTURE §5.3).

Der acoustid-index bekommt **nie** den Vollvektor eines Fingerprints, sondern
einen kleinen, entrauschten Auszug (DECISIONS 2026-07-25 „Fingerprint-Vektoren
in Postgres, Index erhaelt nur Query-Extrakte"). Genau denselben Auszug bildet
die Suche: was beim Indexieren hineingeht, geht beim Nachschlagen wieder
hinein — sonst finden sich die Dokumente nicht wieder.

**Vorlage ist ausschliesslich die C-Funktion** ``acoustid_extract_query``
(Original-Extension pg_acoustid, Datei ``acoustid_compare.c``). Die
offizielle Python-Referenz ist nachweislich defekt (``array('i')`` + unsigned
Werte ⇒ OverflowError) und darf nicht als Vorlage dienen; ein Test vergleicht
diese Funktion bit-genau gegen die Extension
(``shared/tests/test_fingerprint_extension.py``, Phase 9).

Ablauf, genau wie im C-Original:

1. **Stille zaehlen, nicht entfernen.** ``clean_size`` ist die Anzahl der
   Hashes ungleich :data:`SILENCE_HASH` und bestimmt allein den Startoffset.
   Besteht der Vektor nur aus Stille, ist die Query leer.
2. **Startoffset** ``max(0, min(clean_size - max_hashes, 80))`` — dieser
   Index zeigt in den **Originalvektor**, nicht in eine bereinigte Kopie
   (Stille vor dem Offset verschiebt das Fenster also nicht). Der Anfang
   einer Aufnahme ist unzuverlaessig (Intros, Einblendungen), deshalb
   beginnt die Extraktion nach Moeglichkeit bei :data:`QUERY_START_OFFSET`;
   ist der Vektor dafuer zu kurz, ruecken wir nur so weit vor, wie noch
   ``max_hashes`` Hashes uebrig bleiben.
3. **Ab dort durchlaufen:** Silence-Hashes ueberspringen, jeden Wert mit der
   28-Bit-Maske :data:`HASH_MASK` beschneiden (die unteren 4 Bit sind das
   rauschanfaelligste Band), **deduplizieren** und nach ``max_hashes``
   Hashes aufhoeren. Der Index sortiert das Query-Array in-place und zaehlt
   Duplikate doppelt — ein doppelter Hash wuerde den Score verfaelschen.

Alles rechnet **vorzeichenlos**: die Vektoren kommen als signed int32 aus
Postgres bzw. aus den JSONL-Deltas (``-1900322695`` ist ein voellig normaler
Wert), der Index kennt nur u32 und quittiert negative Zahlen mit HTTP 400
``IntegerOverflow``. Das C-Original rechnet auf ``int32`` und liefert
entsprechend negative Zahlen — dieselben Bitmuster.

``max_hashes`` kommt immer vom Aufrufer (Config ``acoustid.index.query_hashes``,
Default 120) — hier steht bewusst kein Default, damit Indexieren und Suchen
nicht versehentlich mit verschiedenen Werten laufen. Im C-Original steht
dafuer die feste Konstante ``ACOUSTID_QUERY_LENGTH`` (120), und zwar an
**beiden** Stellen (Startoffset und Obergrenze) — genau so ist der Parameter
hier eingesetzt.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

__all__ = [
    "HASH_MASK",
    "QUERY_START_OFFSET",
    "SILENCE_HASH",
    "UINT32_MASK",
    "extract_query",
]

#: Chromaprint-Hash eines stillen Abschnitts — traegt keine Information.
SILENCE_HASH: Final = 627964279

#: 28-Bit-Maske: die unteren 4 Bit (rauschanfaelligstes Band) fallen weg.
HASH_MASK: Final = 0xFFFFFFF0

#: Bevorzugter Startoffset im Vektor (``ACOUSTID_QUERY_START``).
QUERY_START_OFFSET: Final = 80

#: Wertebereich des Index: u32.
UINT32_MASK: Final = 0xFFFFFFFF


def extract_query(hashes: Iterable[int], *, max_hashes: int) -> list[int]:
    """Extrahiert die Index-Query aus einem Fingerprint-Vollvektor.

    Args:
        hashes: Vollvektor des Fingerprints. Signed int32 (wie aus Postgres
            bzw. den JSONL-Deltas) ist der Normalfall und wird vorzeichenlos
            interpretiert.
        max_hashes: Obergrenze der zurueckgegebenen Hashes — der Wert aus
            ``acoustid.index.query_hashes``. Eine Aenderung erfordert einen
            Index-Neuaufbau.

    Returns:
        Bis zu ``max_hashes`` maskierte, deduplizierte u32-Hashes in der
        Reihenfolge ihres Auftretens. Die Liste kann kuerzer sein (kurzer
        Vektor, viele Duplikate) oder leer (nur Stille).

    Raises:
        ValueError: ``max_hashes`` ist kleiner als 1.
    """
    if max_hashes < 1:
        raise ValueError(f"max_hashes muss >= 1 sein, war {max_hashes}")

    # Vorzeichenlos machen — dieselbe Speicherstelle wie in C, nur ohne
    # Vorzeichen gelesen.
    values = [value & UINT32_MASK for value in hashes]

    # 1. Stille zaehlen (nicht entfernen): sie bestimmt nur den Startoffset.
    clean_size = sum(1 for value in values if value != SILENCE_HASH)
    if clean_size <= 0:
        return []

    # 2. Startoffset im ORIGINALVEKTOR: so weit wie moeglich nach vorn,
    #    hoechstens bis 80.
    start = max(0, min(clean_size - max_hashes, QUERY_START_OFFSET))

    # 3. Ab dort: Stille ueberspringen, maskieren, deduplizieren, kappen.
    query: list[int] = []
    seen: set[int] = set()
    for value in values[start:]:
        if value == SILENCE_HASH:
            continue
        masked = value & HASH_MASK
        if masked in seen:
            continue
        seen.add(masked)
        query.append(masked)
        if len(query) == max_hashes:
            break
    return query
