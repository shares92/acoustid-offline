-- Gruppe `core`, Migration 0002 — Tabelle `fingerprint` (ARCHITECTURE §5.2).
--
-- Zwei Dump-Stroeme (`fingerprint`, `track_fingerprint`) befuellen disjunkte
-- Spaltenmengen derselben Entitaet, daher sind fachlich verpflichtende
-- Spalten hier bewusst nullable. Kein FK auf `track` (Begruendung in 0001).
--
-- `ALTER … SET COMPRESSION lz4` steht bewusst in der Gruppe `core` und nicht
-- bei den Sekundaerindizes: die Einstellung wirkt nur auf NEU geschriebene
-- Werte. Wuerde sie erst nach dem Bootstrap-Massenimport gesetzt, blieben
-- ausgerechnet die Milliarden Vektoren des Erstimports auf dem langsameren
-- pglz — nachtraeglich nur durch komplettes Neuschreiben der Tabelle
-- korrigierbar.

CREATE TABLE fingerprint (
    id               integer     PRIMARY KEY,        -- == fingerprint_id
    fingerprint      integer[]   NULL,               -- voller signed-int32-Vektor
    length           integer     NULL,               -- Sekunden (upstream small-/integer uneinheitlich)
    track_id         integer     NULL,
    submission_count integer     NULL,
    created          timestamptz NULL,
    updated          timestamptz NULL,
    indexed_at       timestamptz NULL,               -- [P] an acoustid-index uebergeben
    src_day          date        NOT NULL,           -- [P]
    imported_at      timestamptz NOT NULL DEFAULT now() -- [P]
);

ALTER TABLE fingerprint ALTER COLUMN fingerprint SET COMPRESSION lz4;
