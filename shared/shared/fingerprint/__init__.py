"""Fingerprint-Algorithmen von AcoustID, in Python nachgebaut (Phase 9).

Zwei Bausteine der Matching-Pipeline (ARCHITECTURE §5.3), beide **pure**
Rechnung ohne IO und ohne Abhaengigkeiten ausserhalb der Standardbibliothek:

* :mod:`shared.fingerprint.chromaprint` — :func:`decode_fingerprint`, die
  Umrechnung der Chromaprint-Zeichenkette eines Clients in den Vollvektor
  (und :func:`encode_fingerprint` als Gegenstueck).
* :mod:`shared.fingerprint.compare` — :func:`compare2`, der AcoustID-Score
  zweier Vollvektoren; Nachbau von ``acoustid_compare2``.

Der dritte Baustein, :func:`shared.fpindex.extract_query`, liegt bewusst beim
Index-Client: er bestimmt, was in den Suchindex geschrieben wird, und gehoert
damit an dieselbe Stelle wie der Index-Feed. Alle drei werden in CI bit-genau
gegen die Original-C-Extension geprueft (Test-Container, nie produktiv —
DECISIONS 2026-07-25).

Bewusst **nicht** in :mod:`shared` re-exportiert: der Waechter braucht die
Matching-Logik nicht (gleiche Regel wie bei :mod:`shared.db` und
:mod:`shared.fpindex`).
"""

from __future__ import annotations

from shared.fingerprint.chromaprint import (
    FINGERPRINT_VERSION,
    DecodedFingerprint,
    FingerprintDecodeError,
    decode_base64,
    decode_fingerprint,
    encode_fingerprint,
)
from shared.fingerprint.compare import MATCH_BITS, MATCH_MASK, MAX_OFFSET, compare2

__all__ = [
    "FINGERPRINT_VERSION",
    "MATCH_BITS",
    "MATCH_MASK",
    "MAX_OFFSET",
    "DecodedFingerprint",
    "FingerprintDecodeError",
    "compare2",
    "decode_base64",
    "decode_fingerprint",
    "encode_fingerprint",
]
