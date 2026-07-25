-- Gruppe `core`, Migration 0004 — Tabelle `meta` (ARCHITECTURE §5.2).
--
-- Nutzer-Metadaten einer Einreichung. `created` ist upstream nullable und
-- fehlt in den Deltas systematisch (Zeilen mit `created IS NULL` werden nie
-- exportiert) — genau daran scheitert jeder FK von `track_meta` hierher.
-- Die upstream vorhandene Spalte `gid` wird nicht exportiert und fehlt
-- deshalb bewusst.

CREATE TABLE meta (
    id           integer     PRIMARY KEY,
    track        varchar     NULL,
    artist       varchar     NULL,
    album        varchar     NULL,
    album_artist varchar     NULL,
    track_no     integer     NULL,
    disc_no      integer     NULL,
    year         integer     NULL,
    created      timestamptz NULL,                   -- upstream nullable (Delta-Luecke, s. o.)
    src_day      date        NOT NULL,               -- [P]
    imported_at  timestamptz NOT NULL DEFAULT now()  -- [P]
);
