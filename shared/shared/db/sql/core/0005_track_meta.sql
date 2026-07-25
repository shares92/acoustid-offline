-- Gruppe `core`, Migration 0005 — Tabelle `track_meta` (ARCHITECTURE §5.2).
--
-- Verknuepfung Track <-> Nutzer-Metadaten. Kein FK (Begruendung in 0001);
-- fuer `meta_id` ist er zusaetzlich prinzipiell unmoeglich, weil `meta`
-- unvollstaendig exportiert wird.

CREATE TABLE track_meta (
    id               integer     PRIMARY KEY,
    track_id         integer     NOT NULL,
    meta_id          integer     NOT NULL,           -- FK auf meta NICHT erzwingbar
    submission_count integer     NOT NULL,
    created          timestamptz NOT NULL,
    updated          timestamptz NULL,
    src_day          date        NOT NULL,           -- [P]
    imported_at      timestamptz NOT NULL DEFAULT now() -- [P]
);
