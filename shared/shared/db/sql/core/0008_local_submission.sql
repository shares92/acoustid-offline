-- Gruppe `core`, Migration 0008 — Tabelle `local_submission` (Phase 11).
--
-- Quelle: ARCHITECTURE.md §5.2 „Weitere Tabellen" (Spalten werden in ihrer
-- Phase festgelegt) und der Submit-Vertrag aus
-- docs/research/phase1-api-formate.md. Erlaeuterungen: docs/api-submit.md.
--
-- WARUM EINE EIGENE TABELLE. Die sieben Dump-Tabellen gehoeren dem
-- Delta-Importer: sein Upsert schreibt ganze Zeilen per expliziter ID
-- (`INSERT … ON CONFLICT (id) DO UPDATE`, ARCHITECTURE §5.2 Import-Regel 2).
-- Eine lokal eingefuegte `track`- oder `fingerprint`-Zeile wuerde beim
-- naechsten Tagesdelta still ueberschrieben, sobald upstream dieselbe ID
-- vergibt — und upstream vergibt sie garantiert, weil dort dieselben
-- Zaehler laufen. Eigene Einreichungen leben deshalb ausschliesslich hier.
--
-- EINE ZEILE JE MBID. Der Original-Endpunkt nimmt `mbid.N` mehrfach entgegen
-- und erzeugt je MBID eine eigene Submission (eigene ID in der Antwort).
-- Alle Zeilen einer eingereichten Aufnahme teilen sich `local_track_id`
-- (Gruppenschluessel) und `local_track_gid` (die ausgelieferte AcoustID).
--
-- `local_track_id` IST ZUGLEICH DIE DOKUMENT-ID IM SUCHINDEX, versetzt um
-- 2^31: der acoustid-index nimmt u32-Dokument-IDs (empirisch verifiziert,
-- >= 2^32 quittiert er mit HTTP 400 `IntegerOverflow`), und die Dokument-IDs
-- des Delta-Bestands sind `fingerprint.id`, also `integer` <= 2^31-1. Der
-- Bereich [2^31, 2^32-1] ist damit nachweisbar frei. Die Sequenz endet
-- deshalb bei 2147483647 und laeuft NICHT ueber (`NO CYCLE`) — lieber ein
-- lauter Fehler als eine Kollision mit einem importierten Fingerprint.
--
-- Kein Fremdschluessel (Begruendung in 0001), keine Sekundaerindizes (die
-- stehen in Gruppe `indexes`, Datei 0105).

CREATE SEQUENCE local_submission_track_id_seq AS integer MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;

CREATE TABLE local_submission (
    id               bigserial   PRIMARY KEY,           -- [P] Submission-ID der Antwort
    local_track_id   integer     NOT NULL,              -- [P] Gruppe + Dokument-ID (+2^31)
    local_track_gid  uuid        NOT NULL,              -- [P] ausgelieferte AcoustID
    status           text        NOT NULL DEFAULT 'new',-- [P] new|indexed|forwarded|forward_failed
    fingerprint      integer[]   NOT NULL,              -- voller signed-int32-Vektor (Rescoring)
    length           integer     NOT NULL,              -- `duration.N` in Sekunden, 1…32767
    bitrate          integer     NULL,                  -- `bitrate.N`
    fileformat       varchar     NULL,                  -- `fileformat.N`
    mbid             uuid        NULL,                  -- `mbid.N` (eine Zeile je MBID)
    puid             uuid        NULL,                  -- `puid.N` (Legacy)
    foreignid        varchar     NULL,                  -- `foreignid.N`, Form vendor:id
    track            varchar     NULL,                  -- `track.N`
    artist           varchar     NULL,                  -- `artist.N`
    album            varchar     NULL,                  -- `album.N`
    album_artist     varchar     NULL,                  -- `albumartist.N`
    track_no         integer     NULL,                  -- `trackno.N`
    disc_no          integer     NULL,                  -- `discno.N`
    year             integer     NULL,                  -- `year.N`
    client           varchar     NULL,                  -- `client` (Application-Key)
    client_version   varchar     NULL,                  -- `clientversion`
    submitted_by     varchar     NULL,                  -- `user` (Phase 12 reicht ihn durch)
    created          timestamptz NOT NULL DEFAULT now(),
    indexed_at       timestamptz NULL,                  -- [P] an acoustid-index uebergeben
    forwarded_at     timestamptz NULL,                  -- [P] Phase 12
    forward_attempts integer     NOT NULL DEFAULT 0,    -- [P] Phase 12 (Grenze 7, §8.9)
    forward_error    text        NULL,                  -- [P] Phase 12
    CONSTRAINT local_submission_status_check
        CHECK (status IN ('new', 'indexed', 'forwarded', 'forward_failed'))
);

-- Wie bei `fingerprint`: die Kompression wirkt nur auf NEU geschriebene
-- Werte und gehoert deshalb in die Gruppe `core`, nicht zu den Indizes.
ALTER TABLE local_submission ALTER COLUMN fingerprint SET COMPRESSION lz4;
