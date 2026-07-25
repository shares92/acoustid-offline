-- Gruppe `indexes`, Migration 0103 — Sekundaerindizes auf `track_mbid`
-- (ARCHITECTURE §5.2, wortgetreu).

CREATE INDEX track_mbid_idx_track_id ON track_mbid (track_id, mbid); -- bewusst NON-UNIQUE (Merge-Waisen)
CREATE INDEX track_mbid_idx_mbid     ON track_mbid (mbid);
