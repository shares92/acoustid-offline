"""Chromaprint-Dekodierer: Format, Rueckweg, Fehlerfaelle.

Ein Fingerprint kommt als komprimierte, base64-kodierte Zeichenkette herein
und muss als Vollvektor herauskommen — falsch dekodiert findet der Lookup
entweder nichts oder das Falsche. Geprueft wird deshalb dreierlei:

1. **Rueckweg**: Vektor -> Zeichenkette -> Vektor ergibt wieder den Vektor,
   an Randfaellen (leer, ein Wert, grosse Bitabstaende) und an Zufallsdaten.
2. **Format**: Kopf, Bitpackung und die XOR-Verkettung liegen dort, wo das
   Original sie erwartet — belegt an einem von Hand gerechneten Beispiel.
3. **Fehlerfaelle**: alles, was nicht dekodierbar ist, wird als solches
   gemeldet und nicht etwa still zu einem falschen Vektor.
"""

from __future__ import annotations

import base64
import random

import pytest

from shared.fingerprint import (
    FINGERPRINT_VERSION,
    FingerprintDecodeError,
    decode_base64,
    decode_fingerprint,
    encode_fingerprint,
)

_RNG = random.Random(20260725)


def _vector(length: int) -> list[int]:
    return [_RNG.randrange(0, 2**32) for _ in range(length)]


# --- Rueckweg --------------------------------------------------------------


@pytest.mark.parametrize("length", [0, 1, 2, 3, 7, 8, 80, 120, 948, 2000])
def test_roundtrip_reproduces_the_vector(length: int) -> None:
    vector = _vector(length)
    decoded = decode_fingerprint(encode_fingerprint(vector))
    assert decoded.version == FINGERPRINT_VERSION
    assert list(decoded.hashes) == vector


def test_roundtrip_survives_extreme_values() -> None:
    """0, alle Bits, nur das oberste Bit — die Bitabstaende am Rand."""
    vector = [0, 0xFFFFFFFF, 0x80000000, 1, 0, 0xFFFFFFFF]
    assert list(decode_fingerprint(encode_fingerprint(vector)).hashes) == vector


def test_signed_input_is_read_unsigned() -> None:
    """Vektoren aus Postgres sind signed; kodiert wird das Bitmuster."""
    assert encode_fingerprint([-16, -1]) == encode_fingerprint([0xFFFFFFF0, 0xFFFFFFFF])


def test_encoded_form_is_url_safe_without_padding() -> None:
    encoded = encode_fingerprint(_vector(200))
    assert "=" not in encoded
    assert "+" not in encoded
    assert "/" not in encoded


# --- Format ----------------------------------------------------------------


def test_header_carries_version_and_count() -> None:
    raw = decode_base64(encode_fingerprint(_vector(300)))
    assert raw[0] == FINGERPRINT_VERSION
    assert int.from_bytes(raw[1:4], "big") == 300


def test_single_subfingerprint_is_packed_as_documented() -> None:
    """Ein Wert mit gesetztem Bit 1 und Bit 4: Abstaende 1 und 3, dann 0."""
    # 0b1001 -> gesetzte Bits an Position 1 und 4 -> Abstaende 1, 3, Trenner 0.
    # Drei 3-Bit-Werte 1, 3, 0 belegen 9 Bit, also zwei Bytes:
    # 0b00_011_001 = 0x19 und ein Rest-Byte 0x00.
    encoded = encode_fingerprint([0b1001])
    raw = decode_base64(encoded)
    assert raw == bytes([FINGERPRINT_VERSION, 0, 0, 1, 0x19, 0x00])
    assert list(decode_fingerprint(encoded).hashes) == [0b1001]


def test_large_bit_gaps_use_the_five_bit_block() -> None:
    """Ein Abstand >= 7 landet als Ueberlauf im zweiten Block."""
    # Nur Bit 32 gesetzt -> Abstand 32 -> 7 im 3-Bit-Block, 25 im 5-Bit-Block.
    encoded = encode_fingerprint([0x80000000])
    assert list(decode_fingerprint(encoded).hashes) == [0x80000000]


def test_values_are_xor_chained() -> None:
    """Kodiert werden Differenzen — gleiche Werte hintereinander sind billig."""
    same = encode_fingerprint([0x12345678] * 50)
    varied = encode_fingerprint(_vector(50))
    assert len(same) < len(varied)
    assert list(decode_fingerprint(same).hashes) == [0x12345678] * 50


# --- Base64 ----------------------------------------------------------------


def test_padding_is_optional_but_accepted() -> None:
    encoded = encode_fingerprint(_vector(10))
    padded = encoded + "=" * (-len(encoded) % 4)
    assert decode_fingerprint(padded).hashes == decode_fingerprint(encoded).hashes


def test_standard_base64_alphabet_is_rejected() -> None:
    """`+` und `/` sind kein URL-sicheres Base64 — still verschlucken waere schlimmer."""
    raw = bytes([FINGERPRINT_VERSION, 0, 0, 1]) + bytes([0xFB, 0xFF])
    standard = base64.b64encode(raw).decode("ascii").rstrip("=")
    if "+" not in standard and "/" not in standard:  # pragma: no cover - Datenlage
        pytest.skip("Beispiel enthaelt zufaellig keine Sonderzeichen")
    with pytest.raises(FingerprintDecodeError, match="URL-sicher"):
        decode_fingerprint(standard)


@pytest.mark.parametrize("text", ["", "===", "a b", "AAAA!"])
def test_broken_base64_is_reported(text: str) -> None:
    with pytest.raises(FingerprintDecodeError):
        decode_fingerprint(text)


# --- Fehlerfaelle ----------------------------------------------------------


def test_too_short_for_a_header() -> None:
    with pytest.raises(FingerprintDecodeError, match="4 Bytes"):
        decode_fingerprint(base64.urlsafe_b64encode(b"\x01\x00\x00").decode().rstrip("="))


def test_only_version_one_is_supported() -> None:
    """Version 2 gibt es (noch) nicht — das Original lehnt sie ebenfalls ab."""
    raw = bytearray(decode_base64(encode_fingerprint(_vector(5))))
    raw[0] = 2
    encoded = base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")
    with pytest.raises(FingerprintDecodeError, match="Version 2"):
        decode_fingerprint(encoded)


def test_truncated_payload_is_reported() -> None:
    """Abgeschnittene Nutzdaten ergeben keinen halben Vektor, sondern Fehler."""
    raw = decode_base64(encode_fingerprint(_vector(200)))
    cut = base64.urlsafe_b64encode(raw[: len(raw) // 2]).decode("ascii").rstrip("=")
    with pytest.raises(FingerprintDecodeError, match="abgeschnitten"):
        decode_fingerprint(cut)


def test_count_zero_yields_an_empty_vector() -> None:
    """Ein Kopf ohne Nutzdaten ist formal gueltig — und liefert nichts."""
    raw = bytes([FINGERPRINT_VERSION, 0, 0, 0])
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    assert decode_fingerprint(encoded).hashes == ()


def test_impossible_bit_distance_is_reported() -> None:
    """Ein Abstand jenseits von 32 Bit kann kein Chromaprint-Hash sein."""
    # 3-Bit-Werte: 7 (Ueberlauf) ... danach ein 5-Bit-Wert 31 -> Abstand 38.
    header = bytes([FINGERPRINT_VERSION, 0, 0, 1])
    normal = bytes([0b00_000_111])  # 7, dann 0 als Trenner
    exception = bytes([31])
    encoded = base64.urlsafe_b64encode(header + normal + exception).decode("ascii").rstrip("=")
    with pytest.raises(FingerprintDecodeError, match="Bitabstand"):
        decode_fingerprint(encoded)
