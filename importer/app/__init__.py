"""Importer von musicmeta-offline (Import-Name ``acoustid_importer``).

Module (Stand Phase 8 — vollstaendiger One-Shot-Job):

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
``prefetch``            ``Prefetcher``: laedt die naechsten Tagesdateien,
                        waehrend die aktuelle importiert wird.
``bulk``                Bootstrap-Bulk-Modus: unsichere PG-Einstellungen auf
                        Zeit, garantiert zurueckgenommen (§5.2 Regel 6).
``diskguard``           Plattenplatz-Guard vor und waehrend des Laufs (§8.8).
``measure``             DB-, Index- und Durchsatzmessung des Probelaufs.
``report``              Exit-Codes und maschinenlesbarer Ergebnis-Report.
``job``                 ``run``: der komplette One-Shot-Lauf (Bootstrap und
                        taeglicher Import).
``__main__``            Kommandozeile des Containers.
``errors``              Gemeinsame Fehlerhierarchie.
======================  ====================================================

Die Module werden hier bewusst nicht re-exportiert: der Downloader zieht
``httpx``, der Import ``psycopg`` — wer nur parst, soll das nicht laden
muessen.
"""

__version__ = "0.0.1"
