"""Query-Extraktion: Fingerprint-Vollvektor -> Index-Query (ARCHITECTURE §5.3).

Der acoustid-index bekommt **nie** den Vollvektor eines Fingerprints, sondern
einen kleinen, entrauschten Auszug (DECISIONS 2026-07-25 „Fingerprint-Vektoren
in Postgres, Index erhaelt nur Query-Extrakte"). Genau denselben Auszug bildet
die Suche: was beim Indexieren hineingeht, geht beim Nachschlagen wieder
hinein — sonst finden sich die Dokumente nicht wieder.

Vier Schritte, in dieser Reihenfolge (Original: ``acoustid_extract_query``
der C-Extension):

1. **Silence-Hash entfernen.** :data:`SILENCE_HASH` steht fuer Stille und
   traegt keine Information; er wuerde jede Suche mit Zufallstreffern fluten.
2. **Startoffset** ``max(0, min(len(bereinigt) - max_hashes, 80))``. Der
   Anfang einer Aufnahme ist unzuverlaessig (Intros, Einblendungen), deshalb
   beginnt die Extraktion nach Moeglichkeit bei
   :data:`QUERY_START_OFFSET`. Ist der Vektor dafuer zu kurz, ruecken wir nur
   so weit vor, wie noch ``max_hashes`` Hashes uebrig bleiben.
3. **28-Bit-Maske** :data:`HASH_MASK`: die unteren 4 Bit des Chromaprint-Hashes
   sind das rauschanfaelligste Band und werden weggeworfen.
4. **Deduplizieren** und nach ``max_hashes`` Hashes aufhoeren. Der Server
   sortiert das Query-Array in-place und zaehlt Duplikate doppelt — ein
   doppelter Hash wuerde den Score verfaelschen.

Alles rechnet **vorzeichenlos**: die Vektoren kommen als signed int32 aus
Postgres bzw. aus den JSONL-Deltas (``-1900322695`` ist ein voellig normaler
Wert), der Index kennt nur u32 und quittiert negative Zahlen mit HTTP 400
``IntegerOverflow``.

``max_hashes`` kommt immer vom Aufrufer (Config ``index.query_hashes``,
Default 120) — hier steht bewusst kein Default, damit Indexieren und Suchen
nicht versehentlich mit verschiedenen Werten laufen.

Diese Funktion wird in Phase 9 bit-genau gegen die Original-C-Extension
geprueft; die offizielle Python-Referenz von ``extract_query`` ist
nachweislich defekt (``array('i')`` + unsigned Werte) und darf nicht als
Vorlage dienen.
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

#: Bevorzugter Startoffset im bereinigten Vektor (``TRACK_MAX_OFFSET``).
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
            ``index.query_hashes``. Eine Aenderung erfordert einen
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

    # 1. Stille raus; dabei gleich vorzeichenlos machen, damit der Vergleich
    #    denselben Wertebereich trifft wie die C-Fassung.
    cleaned = [value & UINT32_MASK for value in hashes]
    cleaned = [value for value in cleaned if value != SILENCE_HASH]

    # 2. Startoffset: so weit wie moeglich nach vorn, hoechstens bis 80.
    start = max(0, min(len(cleaned) - max_hashes, QUERY_START_OFFSET))

    # 3./4. Maskieren, deduplizieren, bei max_hashes abbrechen.
    query: list[int] = []
    seen: set[int] = set()
    for value in cleaned[start:]:
        masked = value & HASH_MASK
        if masked in seen:
            continue
        seen.add(masked)
        query.append(masked)
        if len(query) == max_hashes:
            break
    return query
