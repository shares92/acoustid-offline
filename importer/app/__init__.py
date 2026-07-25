"""Importer von acoustid-offline (Import-Name ``acoustid_importer``).

Module (Stand Phase 6 — Download und Parser, noch ohne DB-Schreiben):

======================  ====================================================
``streams``             Die sieben Stroeme, ihre Importreihenfolge und das
                        Dateinamens-/URL-Schema (``DeltaFile``).
``records``             Typisierte Records je Strom und ihr Feldvertrag
                        (ARCHITECTURE §5.1).
``parser``              ``DeltaReader``: gzip-JSONL -> Records, Absent-
                        Semantik, Feld-Sanity-Check (§12.8).
``worklist``            Reine Arbeitslisten- und Lueckenlogik
                        (``plan_from_state``), ohne Netz, DB und Uhr.
``download``            ``DeltaDownloader``: Resume per Range, Retries,
                        Groessen- und gzip-Pruefung.
``errors``              Gemeinsame Fehlerhierarchie.
======================  ====================================================

Phase 7 ergaenzt den transaktionalen DB-Import und den Index-Feed, Phase 8
den Bootstrap-Job. Die Module werden hier bewusst nicht re-exportiert: der
Downloader zieht ``httpx``, und wer nur parst, soll das nicht laden muessen.
"""

__version__ = "0.0.1"
