"""Nachbau von ``acoustid_compare2`` in Python (ARCHITECTURE §5.3).

Der acoustid-index liefert nur Kandidaten mit einem Integer-Score
(Trefferzahl im Query-Extrakt). Der **echte** AcoustID-Score entsteht erst
hier: aus dem Vollvektor des Kandidaten (Postgres) und dem Vollvektor der
Anfrage. Weil wir die Original-Extension nicht einsetzen (DECISIONS
2026-07-25 „Rescoring per Python-Nachbau mit CI-Bit-Verifikation"), muss
diese Funktion Zahl fuer Zahl liefern, was die C-Funktion liefern wuerde.

**Quelle:** ``match_fingerprints2`` aus ``acoustid_compare.c`` der
Original-Extension pg_acoustid (die SQL-Funktion ``acoustid_compare2``
prueft nur die Arrays und ruft sie auf). Ein Test vergleicht diese Funktion
bit-genau gegen die laufende Extension in einem Test-Container
(``shared/tests/test_fingerprint_extension.py``); die Extension wird
**nie** produktiv eingesetzt.

Der Ablauf des Originals in Worten:

1. **Grobe Ausrichtung.** Fuer jeden 14-Bit-Praefix eines Hashes merkt sich
   das Original die **zuletzt** gesehene Position in ``a`` bzw. ``b``
   (zwei Tabellen mit 16384 Eintraegen). Kommt ein Praefix in beiden vor,
   ist die Positionsdifferenz ein Kandidat fuer den Zeitversatz; der
   haeufigste Versatz gewinnt (``topoffset``).
2. **Verschieben.** Der laengere Vorlauf wird abgeschnitten, sodass beide
   Vektoren am gemeinsamen Anfang stehen.
3. **Vielfalt (``diversity``).** Wie viele verschiedene 14-Bit-Praefixe
   enthalten die verschobenen Vektoren? Wenig Vielfalt (viele Wiederholungen,
   z. B. Stille oder Loops) heisst: der Bitvergleich unten ist zu optimistisch,
   der Score wird potenziert gedaempft.
4. **Bitvergleich.** Ueber die gemeinsame Laenge (auf gerade Anzahl
   abgerundet) werden die Bitfehler gezaehlt; daraus entsteht der Score.

**Bit-genau heisst auch: Eigenheiten des Originals mitnehmen.** Drei davon
sind hier bewusst nachgebaut und im Code markiert:

* ``UNIQ_STRIP``/``UNIQ_MASK`` sind im Original als 16-Bit-Groessen gedacht,
  benutzen im Makro aber ``MATCH_BITS`` — es sind also dieselben 14 Bit.
* Die Ausrichtungsschleife laeuft ``for (i = 0; i < MATCH_MASK; i++)``, der
  hoechste Praefix (16383) bleibt bei der Ausrichtung also aussen vor; und
  eine gespeicherte Position ``0`` gilt als „nicht gesetzt", das erste
  Element eines Vektors ist fuer die Ausrichtung damit unsichtbar.
* Der ``seen``-Puffer der Vielfaltszaehlung liegt im selben Speicher wie die
  Positionstabelle und wird nur ueber ``UNIQ_MASK`` (= 16383) Bytes
  geloescht — Byte 16383 (Praefix 16383) behaelt also seinen Altwert aus der
  Positionstabelle und ueberlebt ausserdem den zweiten ``memset``. Das ist
  in :func:`_count_unique` nachgebildet.

**Fliesskomma.** Das Original rechnet in ``double`` und speichert in
``float4``; jede Zuweisung an eine ``float``-Variable rundet also auf
einfache Genauigkeit. :func:`_f32` macht genau diese Rundungen sichtbar. Die
Reihenfolge der Rundungen ist Teil des Ergebnisses und darf nicht
zusammengefasst werden.
"""

from __future__ import annotations

import math
import struct
from array import array
from collections.abc import Sequence
from typing import Final

__all__ = [
    "MATCH_BITS",
    "MATCH_MASK",
    "MAX_OFFSET",
    "compare2",
]

#: ``MATCH_BITS`` — Laenge des Hash-Praefix, ueber den ausgerichtet wird.
MATCH_BITS: Final = 14

#: ``MATCH_MASK`` — zugleich Obergrenze der Ausrichtungsschleife (exklusiv)
#: und Groesse des geloeschten ``seen``-Bereichs (``UNIQ_MASK``).
MATCH_MASK: Final = (1 << MATCH_BITS) - 1

#: Rechtsschiebung fuer ``MATCH_STRIP``/``UNIQ_STRIP``.
_MATCH_SHIFT: Final = 32 - MATCH_BITS

#: Index in der Positionstabelle, dessen oberes Byte spaeter als
#: ``seen[MATCH_MASK]`` weitergelesen wird (Little-Endian-uint16-Puffer).
_ALIAS_SLOT: Final = MATCH_MASK // 2

#: ``TRACK_MAX_OFFSET`` aus ``acoustid/const.py`` — der Wert, mit dem der
#: Lookup die Funktion aufruft.
MAX_OFFSET: Final = 80

_UINT32_MASK: Final = 0xFFFFFFFF


def _f32(value: float) -> float:
    """Rundet auf einfache Genauigkeit — eine Zuweisung an ``float4`` in C."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _as_u32(values: Sequence[int]) -> array[int]:
    """Vektor als vorzeichenloses ``uint32``-Array (signed int32 erlaubt)."""
    if isinstance(values, array) and values.typecode == "I":
        return values
    return array("I", [value & _UINT32_MASK for value in values])


def _count_unique(values: array[int], alias_byte: int) -> tuple[int, int]:
    """Zaehlt verschiedene 14-Bit-Praefixe — inklusive Speicher-Eigenheit.

    Das Original benutzt als ``seen``-Tabelle denselben Puffer wie die
    Positionstabelle und loescht davor nur ``UNIQ_MASK`` (= 16383) **Bytes**.
    Das Byte mit Index 16383 — also genau der Praefix ``MATCH_MASK`` — wird
    dabei nie geloescht: es traegt beim ersten Durchlauf noch das obere Byte
    von ``aoffsets[8191]`` und ueberlebt beim zweiten Durchlauf das Ergebnis
    des ersten. ``alias_byte`` fuehrt genau dieses eine Byte mit.

    Args:
        values: Der (bereits verschobene) Vektor.
        alias_byte: Aktueller Wert von ``seen[MATCH_MASK]``.

    Returns:
        Anzahl verschiedener Praefixe und der fortgeschriebene ``alias_byte``.
    """
    seen: set[int] = set()
    unique = 0
    for value in values:
        key = value >> _MATCH_SHIFT
        if key == MATCH_MASK:
            if not alias_byte:
                unique += 1
                alias_byte = 1
        elif key not in seen:
            seen.add(key)
            unique += 1
    return unique, alias_byte


def compare2(a: Sequence[int], b: Sequence[int], max_offset: int = 0) -> float:
    """AcoustID-Score zweier Fingerprint-Vollvektoren, wie ``acoustid_compare2``.

    Args:
        a: Erster Vollvektor. Im Lookup ist das der **gespeicherte**
            Fingerprint aus Postgres — die Reihenfolge der Argumente ist
            nicht symmetrisch und folgt dem Original-SQL
            (``acoustid_compare2(fingerprint, query, max_offset)``).
        b: Zweiter Vollvektor (im Lookup der angefragte Fingerprint).
        max_offset: Groesster zugelassener Zeitversatz in Vektorpositionen;
            ``0`` heisst „unbegrenzt". Der Lookup benutzt
            :data:`MAX_OFFSET` (80).

    Returns:
        Score zwischen 0.0 und 1.0, exakt der ``float4``-Wert der
        C-Funktion. Ein leerer Vektor ergibt 0.0 (im Original faengt das die
        ``ARRISVOID``-Pruefung der SQL-Huelle ab).
    """
    au = _as_u32(a)
    bu = _as_u32(b)
    a_size = len(au)
    b_size = len(bu)
    if a_size == 0 or b_size == 0:
        return 0.0

    # --- 1. Grobe Ausrichtung ---------------------------------------------
    # Positionstabellen: je Praefix die ZULETZT gesehene Position, als uint16
    # (das Original speichert in `uint16_t`, laengere Vektoren laufen ueber).
    a_offsets: dict[int, int] = {}
    for position, value in enumerate(au):
        a_offsets[value >> _MATCH_SHIFT] = position & 0xFFFF
    b_offsets: dict[int, int] = {}
    for position, value in enumerate(bu):
        b_offsets[value >> _MATCH_SHIFT] = position & 0xFFFF

    # Das obere Byte von aoffsets[8191] wird spaeter als seen[MATCH_MASK]
    # weitergelesen (siehe _count_unique).
    alias_byte = (a_offsets.get(_ALIAS_SLOT, 0) >> 8) & 0xFF

    counts: dict[int, int] = {}
    top_count = 0
    top_offset = 0
    # Die C-Schleife laeuft aufsteigend und nur bis MATCH_MASK (exklusiv);
    # bei Gleichstand gewinnt deshalb der zuerst erreichte Versatz.
    for key in sorted(a_offsets.keys() & b_offsets.keys()):
        if key >= MATCH_MASK:
            continue
        a_position = a_offsets[key]
        b_position = b_offsets[key]
        # Eine gespeicherte 0 ist im Original nicht von „nie gesehen"
        # unterscheidbar (`if (aoffsets[i] && boffsets[i])`).
        if a_position == 0 or b_position == 0:
            continue
        offset = a_position - b_position
        if max_offset != 0 and not (-max_offset <= offset <= max_offset):
            continue
        offset += b_size
        count = counts.get(offset, 0) + 1
        counts[offset] = count
        if count > top_count:
            top_count = count
            top_offset = offset
    top_offset -= b_size

    # --- 2. Verschieben ----------------------------------------------------
    min_size = min(a_size, b_size) & ~1  # vor dem Verschieben gemessen
    if top_offset < 0:
        bu = bu[-top_offset:]
        b_size = max(0, b_size + top_offset)
    else:
        au = au[top_offset:]
        a_size = max(0, a_size - top_offset)

    size = min(a_size, b_size) // 2
    if size == 0 or min_size == 0:
        # „empty matching subfingerprint" — kein gemeinsamer Abschnitt.
        return 0.0

    # --- 3. Vielfalt -------------------------------------------------------
    a_unique, alias_byte = _count_unique(au, alias_byte)
    b_unique, alias_byte = _count_unique(bu, alias_byte)
    diversity = _f32(
        min(
            min(1.0, _f32((a_unique + 10) / a_size) + 0.5),
            min(1.0, _f32((b_unique + 10) / b_size) + 0.5),
        )
    )

    if top_count < max(a_unique, b_unique) * 0.02:
        # Der beste Versatz erklaert weniger als 2 % der Vielfalt — das ist
        # Rauschen, kein Treffer.
        return 0.0

    # --- 4. Bitvergleich ---------------------------------------------------
    # Das Original liest je zwei int32 als ein uint64 und zaehlt die Bits von
    # `*adata ^ *bdata`. Ueber den ganzen Abschnitt ist das dasselbe wie die
    # Bitzahl des XOR beider Byteketten — nur in einem Rutsch.
    span = size * 2
    error_bits = (
        int.from_bytes(au[:span].tobytes(), "little")
        ^ int.from_bytes(bu[:span].tobytes(), "little")
    ).bit_count()

    score = _f32((size * 2.0 / min_size) * (1.0 - 2.0 * _f32(error_bits) / (64 * size)))
    if score < 0.0:
        score = 0.0
    if diversity < 1.0:
        score = _f32(math.pow(score, 8.0 - 7.0 * diversity))
    return score
