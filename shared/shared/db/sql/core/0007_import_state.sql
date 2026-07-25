-- Gruppe `core`, Migration 0007 — Tabelle `import_state` (ARCHITECTURE §5.2).
--
-- Rein projektspezifisch [P]: Buchfuehrung des resumierbaren Imports, je
-- Strom und Kalendertag eine Zeile. Grundlage der Lueckenpruefung
-- (`generate_series` gegen diese Tabelle) und des Wiederanlaufs nach einem
-- Abbruch: `finished_at IS NULL` heisst "Tag nicht sauber abgeschlossen".

CREATE TABLE import_state (                          -- [P] Buchfuehrung resumierbarer Import
    stream      text        NOT NULL,                -- 'track' | 'fingerprint' | ...
    day         date        NOT NULL,
    file_name   text        NOT NULL,
    file_size   bigint      NULL,
    row_count   bigint      NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz NULL,
    PRIMARY KEY (stream, day)
);
