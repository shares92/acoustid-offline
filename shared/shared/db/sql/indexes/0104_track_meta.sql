-- Gruppe `indexes`, Migration 0104 — Sekundaerindex auf `track_meta`
-- (ARCHITECTURE §5.2, wortgetreu).
--
-- `meta` und `track_puid` bekommen bewusst keinen Sekundaerindex: die eine
-- wird ausschliesslich ueber den Primaerschluessel gelesen, die andere
-- bleibt praktisch leer.

CREATE INDEX track_meta_idx_track_id ON track_meta (track_id);
