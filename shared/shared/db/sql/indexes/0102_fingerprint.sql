-- Gruppe `indexes`, Migration 0102 — Sekundaerindizes auf `fingerprint`
-- (ARCHITECTURE §5.2, wortgetreu).
--
-- `fingerprint_idx_incomplete` findet Zeilen, die nur einer der beiden
-- Stroeme geschrieben hat; nach einem vollstaendigen Replay ist der Index
-- leer und kostet nichts. `fingerprint_idx_unindexed` ist der Arbeitsvorrat
-- des Index-Feeds (aufsteigend nach id abgearbeitet).

CREATE INDEX fingerprint_idx_track_id   ON fingerprint (track_id);
CREATE INDEX fingerprint_idx_incomplete ON fingerprint (id)
    WHERE fingerprint IS NULL OR track_id IS NULL;   -- [P] nach Voll-Replay leer!
CREATE INDEX fingerprint_idx_unindexed  ON fingerprint (id)
    WHERE indexed_at IS NULL;                        -- [P] Arbeitsvorrat Index-Feed
