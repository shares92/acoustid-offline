-- Gruppe `indexes`, Migration 0105 — Sekundaerindizes fuer `local_submission`
-- (Tabelle: core/0008, Erlaeuterungen: docs/api-submit.md).
--
-- `local_submission_idx_unindexed` ist der Arbeitsvorrat der Statusmaschine
-- (Muster `fingerprint_idx_unindexed`): jede Submit-Anfrage traegt die noch
-- nicht indexierten Einreichungen nach, und dieser Partialindex haelt die
-- Abfrage klein — ohne ihn waere sie ein Seq-Scan ueber alle je
-- eingereichten Zeilen, obwohl im Normalbetrieb keine einzige offen ist.
--
-- `local_submission_idx_track_id` bedient den Lookup: der Suchindex liefert
-- eine Dokument-ID aus dem reservierten Bereich, daraus wird die
-- `local_track_id`, und dazu werden Vektor, AcoustID und MBIDs geholt.
--
-- `local_submission_idx_track_gid` bedient den Parameter `trackid` (eine
-- lokale AcoustID laesst sich damit direkt nachschlagen). Bewusst NICHT
-- unique: alle Zeilen einer eingereichten Aufnahme teilen sich die GID.

CREATE INDEX local_submission_idx_unindexed ON local_submission (id)
    WHERE status = 'new';                                -- [P] Arbeitsvorrat Indexierung
CREATE INDEX local_submission_idx_track_id  ON local_submission (local_track_id);
CREATE INDEX local_submission_idx_track_gid ON local_submission (local_track_gid);
