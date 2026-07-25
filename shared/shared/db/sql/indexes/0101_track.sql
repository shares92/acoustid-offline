-- Gruppe `indexes`, Migration 0101 — Sekundaerindizes auf `track`
-- (ARCHITECTURE §5.2, wortgetreu).
--
-- Die ganze Gruppe wird beim Bootstrap erst NACH dem Massenimport angewendet
-- (ARCHITECTURE §5.2 Import-Regel 6): Indexpflege waehrend des Voll-Replays
-- kostet ein Vielfaches des Nachbauens.
--
-- `track_idx_gid` ist der Lookup-Weg der oeffentlichen AcoustID und deshalb
-- UNIQUE; `track_idx_new_id` ist partiell, weil nur gemergte Tracks ein
-- Merge-Ziel haben (der Index bleibt damit winzig).

CREATE UNIQUE INDEX track_idx_gid    ON track (gid);
CREATE INDEX  track_idx_new_id ON track (new_id) WHERE new_id IS NOT NULL;
