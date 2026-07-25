"""Chromaprint-Fingerprints dekodieren (ARCHITECTURE §7, Phase-1-Vertrag).

Clients schicken den Fingerprint als **komprimierte Chromaprint-Zeichenkette**
in URL-sicherem Base64 ohne Padding. Bevor irgendetwas gesucht werden kann,
muss daraus wieder der Vollvektor aus u32-Hashes werden: er geht sowohl in die
Query-Extraktion (:func:`shared.fpindex.extract_query`) als auch in das
Rescoring (:func:`shared.fingerprint.compare2`).

**Quelle:** ``FingerprintDecompressor`` aus ``chromaprint``
(``src/fingerprint_decompressor.cpp`` samt ``utils/unpack_int3_array.h`` und
``utils/unpack_int5_array.h``, MIT-Lizenz). Das Format:

===========  ==========================================================
Byte 0       Algorithmus-/Versionsnummer. Nur ``1`` ist zugelassen
             (:data:`FINGERPRINT_VERSION`) — alles andere lehnt schon das
             Original ab.
Byte 1..3    Anzahl der Subfingerprints, Big-Endian.
ab Byte 4    Dicht gepackte 3-Bit-Werte: je Subfingerprint die Abstaende
             der gesetzten Bits, mit ``0`` als Trenner. Der Wert ``7``
             heisst „Abstand >= 7" und wird unten fortgesetzt.
danach       Dicht gepackte 5-Bit-Werte: die Ueberlaeufe zu jeder ``7``,
             in derselben Reihenfolge.
===========  ==========================================================

Die Subfingerprints sind **XOR-verkettet**: dekodiert wird jeweils die
Differenz zum Vorgaenger, der laufende XOR-Wert ist der Hash. Deshalb ist der
Vollvektor nur als Ganzes lesbar — ein Teilstueck ergibt keine gueltigen
Hashes.

Alles hier ist pure Rechnung ohne IO; Fehler sind
:class:`FingerprintDecodeError` (der Aufrufer macht daraus den AcoustID-Fehler
3 „invalid fingerprint").
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "FINGERPRINT_VERSION",
    "DecodedFingerprint",
    "FingerprintDecodeError",
    "decode_base64",
    "decode_fingerprint",
    "encode_fingerprint",
]

#: Einzige unterstuetzte Chromaprint-Version (wie im Original).
FINGERPRINT_VERSION: Final = 1

#: Kopfgroesse: 1 Byte Version + 3 Byte Anzahl.
_HEADER_SIZE: Final = 4

#: ``kNormalBits`` / ``kExceptionBits`` / ``kMaxNormalValue``.
_NORMAL_BITS: Final = 3
_EXCEPTION_BITS: Final = 5
_MAX_NORMAL_VALUE: Final = (1 << _NORMAL_BITS) - 1

#: Ein Hash hat 32 Bit — ein groesserer Bitabstand kann nicht vorkommen und
#: bedeutet eine kaputte Zeichenkette (in C waere es undefiniertes Verhalten).
_HASH_BITS: Final = 32

#: URL-sicheres Base64-Alphabet (RFC 4648 §5). Bewusst als Menge geprueft:
#: ``base64.urlsafe_b64decode`` wirft fremde Zeichen still weg, und ein
#: klammheimlich anderer Fingerprint waere schlimmer als eine Fehlermeldung.
_B64_ALPHABET: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class FingerprintDecodeError(Exception):
    """Die Zeichenkette ist kein gueltiger Chromaprint-Fingerprint."""


@dataclass(frozen=True, slots=True)
class DecodedFingerprint:
    """Ergebnis von :func:`decode_fingerprint`.

    Attributes:
        version: Algorithmusnummer aus dem Kopf (immer
            :data:`FINGERPRINT_VERSION`, sonst gaebe es einen Fehler).
        hashes: Vollvektor als u32-Hashes in Originalreihenfolge.
    """

    version: int
    hashes: tuple[int, ...]


def decode_base64(text: str) -> bytes:
    """URL-sicheres Base64 ohne Padding -> Bytes.

    Padding (``=``) ist zugelassen, aber nicht noetig; alles ausserhalb des
    URL-sicheren Alphabets ist ein Fehler.

    Raises:
        FingerprintDecodeError: Fremdes Zeichen oder unmoegliche Laenge.
    """
    stripped = text.rstrip("=")
    if not stripped:
        raise FingerprintDecodeError("leere Zeichenkette")
    invalid = set(stripped) - _B64_ALPHABET
    if invalid:
        raise FingerprintDecodeError(
            "unerlaubte Zeichen fuer URL-sicheres Base64: " + "".join(sorted(invalid))
        )
    try:
        return base64.urlsafe_b64decode(stripped + "=" * (-len(stripped) % 4))
    except (binascii.Error, ValueError) as exc:  # unmoegliche Restlaenge
        raise FingerprintDecodeError(f"kein gueltiges Base64: {exc}") from exc


def _unpack_int3(data: bytes) -> list[int]:
    """Entpackt dicht gepackte 3-Bit-Werte (``UnpackInt3Array``)."""
    values: list[int] = []
    append = values.append
    position = 0
    size = len(data)
    while size - position >= 3:
        s0, s1, s2 = data[position], data[position + 1], data[position + 2]
        position += 3
        append(s0 & 0x07)
        append((s0 & 0x38) >> 3)
        append(((s0 & 0xC0) >> 6) | ((s1 & 0x01) << 2))
        append((s1 & 0x0E) >> 1)
        append((s1 & 0x70) >> 4)
        append(((s1 & 0x80) >> 7) | ((s2 & 0x03) << 1))
        append((s2 & 0x1C) >> 2)
        append((s2 & 0xE0) >> 5)
    rest = size - position
    if rest == 2:
        s0, s1 = data[position], data[position + 1]
        append(s0 & 0x07)
        append((s0 & 0x38) >> 3)
        append(((s0 & 0xC0) >> 6) | ((s1 & 0x01) << 2))
        append((s1 & 0x0E) >> 1)
        append((s1 & 0x70) >> 4)
    elif rest == 1:
        s0 = data[position]
        append(s0 & 0x07)
        append((s0 & 0x38) >> 3)
    return values


def _unpack_int5(data: bytes) -> list[int]:
    """Entpackt dicht gepackte 5-Bit-Werte (``UnpackInt5Array``)."""
    values: list[int] = []
    append = values.append
    position = 0
    size = len(data)
    while size - position >= 5:
        s0, s1, s2, s3, s4 = data[position : position + 5]
        position += 5
        append(s0 & 0x1F)
        append(((s0 & 0xE0) >> 5) | ((s1 & 0x03) << 3))
        append((s1 & 0x7C) >> 2)
        append(((s1 & 0x80) >> 7) | ((s2 & 0x0F) << 1))
        append(((s2 & 0xF0) >> 4) | ((s3 & 0x01) << 4))
        append((s3 & 0x3E) >> 1)
        append(((s3 & 0xC0) >> 6) | ((s4 & 0x07) << 2))
        append((s4 & 0xF8) >> 3)
    rest = size - position
    if rest == 4:
        s0, s1, s2, s3 = data[position : position + 4]
        append(s0 & 0x1F)
        append(((s0 & 0xE0) >> 5) | ((s1 & 0x03) << 3))
        append((s1 & 0x7C) >> 2)
        append(((s1 & 0x80) >> 7) | ((s2 & 0x0F) << 1))
        append(((s2 & 0xF0) >> 4) | ((s3 & 0x01) << 4))
        append((s3 & 0x3E) >> 1)
    elif rest == 3:
        s0, s1, s2 = data[position : position + 3]
        append(s0 & 0x1F)
        append(((s0 & 0xE0) >> 5) | ((s1 & 0x03) << 3))
        append((s1 & 0x7C) >> 2)
        append(((s1 & 0x80) >> 7) | ((s2 & 0x0F) << 1))
    elif rest == 2:
        s0, s1 = data[position : position + 2]
        append(s0 & 0x1F)
        append(((s0 & 0xE0) >> 5) | ((s1 & 0x03) << 3))
        append((s1 & 0x7C) >> 2)
    elif rest == 1:
        append(data[position] & 0x1F)
    return values


def _packed_size(count: int, bits: int) -> int:
    """Bytes, die ``count`` Werte à ``bits`` Bit dicht gepackt belegen."""
    return (count * bits + 7) // 8


def _pack(values: list[int], bits: int) -> bytes:
    """Packt Werte dicht, niederwertiges Bit zuerst (``PackInt3/5Array``).

    Die aufgerollten Fassungen im Original sind nichts anderes als dieser
    Bitstrom; die Restbits am Ende werden mit Nullen aufgefuellt.
    """
    out = bytearray()
    accumulator = 0
    pending = 0
    mask = (1 << bits) - 1
    for value in values:
        accumulator |= (value & mask) << pending
        pending += bits
        while pending >= 8:
            out.append(accumulator & 0xFF)
            accumulator >>= 8
            pending -= 8
    if pending:
        out.append(accumulator & 0xFF)
    return bytes(out)


def decode_fingerprint(text: str) -> DecodedFingerprint:
    """Dekodiert eine Chromaprint-Zeichenkette zum Vollvektor.

    Args:
        text: Der Wert des Parameters ``fingerprint`` — komprimierter
            Chromaprint in URL-sicherem Base64 ohne Padding.

    Returns:
        Version und Vollvektor. Ein Fingerprint **ohne** Subfingerprints ist
        formal gueltig und ergibt eine leere ``hashes``-Folge; der Aufrufer
        behandelt ihn wie das Original als ungueltig.

    Raises:
        FingerprintDecodeError: Base64 kaputt, Kopf zu kurz, unbekannte
            Version, abgeschnittene Nutzdaten oder unmoeglicher Bitabstand.
    """
    data = decode_base64(text)
    if len(data) < _HEADER_SIZE:
        raise FingerprintDecodeError(f"kuerzer als {_HEADER_SIZE} Bytes")

    version = data[0]
    if version != FINGERPRINT_VERSION:
        raise FingerprintDecodeError(
            f"Fingerprint-Version {version} wird nicht unterstuetzt "
            f"(erwartet {FINGERPRINT_VERSION})"
        )
    count = int.from_bytes(data[1:4], "big")

    bits = _unpack_int3(data[_HEADER_SIZE:])

    # Bis zum `count`-ten Trenner lesen; alles danach ist Fuellung der
    # Bitpackung bzw. schon der 5-Bit-Block.
    found = 0
    exceptions = 0
    end = 0
    for position, bit in enumerate(bits):
        if bit == 0:
            found += 1
            if found == count:
                end = position + 1
                break
        elif bit == _MAX_NORMAL_VALUE:
            exceptions += 1
    if found != count:
        raise FingerprintDecodeError(
            f"abgeschnitten: {found} von {count} Subfingerprints im 3-Bit-Block"
        )
    bits = bits[:end]

    if exceptions:
        offset = _HEADER_SIZE + _packed_size(len(bits), _NORMAL_BITS)
        needed = _packed_size(exceptions, _EXCEPTION_BITS)
        if len(data) < offset + needed:
            raise FingerprintDecodeError("abgeschnitten: 5-Bit-Block unvollstaendig")
        overflow = _unpack_int5(data[offset : offset + needed])
        index = 0
        for position, bit in enumerate(bits):
            if bit == _MAX_NORMAL_VALUE:
                bits[position] = bit + overflow[index]
                index += 1

    hashes: list[int] = []
    value = 0
    last_bit = 0
    for bit in bits:
        if bit == 0:
            # Ein Subfingerprint ist fertig. `value` wird bewusst NICHT
            # zurueckgesetzt: es traegt die XOR-Verkettung weiter.
            hashes.append(value)
            last_bit = 0
            continue
        last_bit += bit
        if last_bit > _HASH_BITS:
            raise FingerprintDecodeError(
                f"Bitabstand {last_bit} liegt ausserhalb eines {_HASH_BITS}-Bit-Hashes"
            )
        value ^= 1 << (last_bit - 1)

    return DecodedFingerprint(version=version, hashes=tuple(hashes))


def encode_fingerprint(hashes: Sequence[int], version: int = FINGERPRINT_VERSION) -> str:
    """Gegenstueck zu :func:`decode_fingerprint` (``FingerprintCompressor``).

    Gebraucht wird der Weg zurueck, sobald ein gespeicherter Vollvektor wieder
    als Chromaprint-Zeichenkette gebraucht wird (eigene Einreichungen,
    Phase 11/12) — und er macht den Dekodierer ohne fremde Bibliothek
    pruefbar: ein Vektor, der durch beide Richtungen laeuft, muss sich selbst
    ergeben.

    Args:
        hashes: Vollvektor; signed int32 wird vorzeichenlos gelesen.
        version: Algorithmusnummer fuer das erste Byte.

    Returns:
        URL-sicheres Base64 ohne Padding — genau das Format, das Clients
        schicken.
    """
    normal: list[int] = []
    overflow: list[int] = []
    previous = 0
    for raw in hashes:
        value = (raw ^ previous) & 0xFFFFFFFF
        previous = raw & 0xFFFFFFFF
        bit = 1
        last_bit = 0
        while value:
            if value & 1:
                distance = bit - last_bit
                if distance >= _MAX_NORMAL_VALUE:
                    normal.append(_MAX_NORMAL_VALUE)
                    overflow.append(distance - _MAX_NORMAL_VALUE)
                else:
                    normal.append(distance)
                last_bit = bit
            value >>= 1
            bit += 1
        normal.append(0)

    count = len(hashes)
    header = bytes([version & 0xFF, (count >> 16) & 0xFF, (count >> 8) & 0xFF, count & 0xFF])
    body = _pack(normal, _NORMAL_BITS) + _pack(overflow, _EXCEPTION_BITS)
    return base64.urlsafe_b64encode(header + body).decode("ascii").rstrip("=")
