-- Gruppe `core`, Migration 0003 — Tabelle `track_mbid` (ARCHITECTURE §5.2).
--
-- Verknuepfung AcoustID <-> MusicBrainz-Recording. `disabled` fehlt im Dump,
-- sobald es false ist (json_strip_nulls) — der Importer setzt es deshalb
-- immer explizit, statt es "unveraendert" zu lassen. Kein FK auf `track`
-- (Begruendung in 0001).

CREATE TABLE track_mbid (
    id               integer     PRIMARY KEY,
    track_id         integer     NOT NULL,
    mbid             uuid        NOT NULL,           -- MusicBrainz-Recording
    submission_count integer     NOT NULL,
    disabled         boolean     NOT NULL DEFAULT false, -- Schluessel fehlt => false setzen
    created          timestamptz NOT NULL,
    updated          timestamptz NULL,
    src_day          date        NOT NULL,           -- [P]
    imported_at      timestamptz NOT NULL DEFAULT now() -- [P]
);
