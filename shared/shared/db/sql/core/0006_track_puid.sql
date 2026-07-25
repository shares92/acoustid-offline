-- Gruppe `core`, Migration 0006 — Tabelle `track_puid` (ARCHITECTURE §5.2).
--
-- Legacy-Strom (MusicIP-PUIDs). Bleibt praktisch leer, laeuft aber in der
-- Lueckenpruefung des Importers mit und wird deshalb regulaer angelegt.
-- Kein FK (Begruendung in 0001).

CREATE TABLE track_puid (                            -- Legacy; bleibt praktisch leer,
    id               integer     PRIMARY KEY,        -- Strom laeuft in der Lueckenpruefung mit
    track_id         integer     NOT NULL,
    puid             uuid        NOT NULL,
    submission_count integer     NOT NULL,
    created          timestamptz NOT NULL,
    updated          timestamptz NULL,
    src_day          date        NOT NULL,           -- [P]
    imported_at      timestamptz NOT NULL DEFAULT now() -- [P]
);
