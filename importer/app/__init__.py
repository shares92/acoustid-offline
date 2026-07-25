"""Importer von acoustid-offline (Import-Name ``acoustid_importer``).

Module (Stand Phase 7 — Download, Parser, DB-Import und Index-Feed):

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
``upserts``             Ein ``INSERT … ON CONFLICT`` je Strom samt
                        Record-Uebersetzung — pure Logik (§5.2 Regel 2/3).
``state``               Buchfuehrung ``import_state``; verbindet die
                        Arbeitsliste mit der Datenbank (§8.4).
``dbimport``            ``import_file``: eine Tagesdatei = eine Transaktion
                        inkl. ``import_state`` (§8.3/§8.4).
``indexfeed``           ``feed_index``: neue Fingerprints als Query-Extrakt
                        in den acoustid-index (§5.3).
``errors``              Gemeinsame Fehlerhierarchie.
======================  ====================================================

Phase 8 ergaenzt den Bootstrap-Bulk-Modus, den Platz-Guard und den
One-Shot-Job-Rumpf. Die Module werden hier bewusst nicht re-exportiert: der
Downloader zieht ``httpx``, der Import ``psycopg`` — wer nur parst, soll
das nicht laden muessen.
"""

__version__ = "0.0.1"
