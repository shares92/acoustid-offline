-- Gruppe `core`, Migration 0001 — Tabelle `track` (eine Zeile je AcoustID).
--
-- Quelle: ARCHITECTURE.md §5.2. Das dortige DDL ist verbindlich und hier
-- wortgetreu uebernommen; ein Test vergleicht beide Staende Anweisung fuer
-- Anweisung. Spalten ohne Marke stammen aus dem Dump [D], `src_day` und
-- `imported_at` sind projektspezifische Buchfuehrung [P].
--
-- KEINE FREMDSCHLUESSEL im gesamten Schema — bewusst (ARCHITECTURE §5.2):
-- Die sieben Tagesstroeme werden unabhaengig voneinander importiert und
-- verweisen ueber Tagesgrenzen hinweg aufeinander; `meta`-Zeilen mit
-- `created IS NULL` erscheinen nie im Delta, weshalb `track_meta.meta_id`
-- gegen `meta.id` prinzipiell nicht erzwingbar ist. Dazu kommen
-- Merge-Waisen (`track_mbid` auf bereits gemergte Tracks) und die
-- prinzipbedingte Vor-2011-Luecke des Voll-Replays. Referenzielle
-- Integritaet stellt der Importer per Reihenfolge und Upsert her, nicht die
-- Datenbank.

CREATE TABLE track (
    id          integer      PRIMARY KEY,
    gid         uuid         NOT NULL,              -- oeffentliche AcoustID
    new_id      integer      NULL,                  -- Merge-Ziel (ggf. verkettet)
    created     timestamptz  NOT NULL,
    updated     timestamptz  NULL,
    src_day     date         NOT NULL,              -- [P] Tagesdatei der letzten Anwendung
    imported_at timestamptz  NOT NULL DEFAULT now() -- [P]
);
