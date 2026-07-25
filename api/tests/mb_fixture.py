"""MusicBrainz-Mini-Schema fuer die Integrationstests (Phase 10).

Die echten MusicBrainz-Dumps sind zweistellige Gigabyte und lizenzrechtlich
nichts fuers Repo. Fuer die Frage, um die es hier geht — **stimmt unser SQL
gegen das echte Schema?** — reichen die 17 Relationen mit genau den
Spalten, die :data:`shared.mb.queries.EXPECTED_COLUMNS` erwartet, und eine
Handvoll synthetischer Zeilen.

Wichtig ist, dass die **Typen** stimmen: ``gid uuid`` (sonst laufen die
``::uuid[]``-Casts ins Leere), ``length integer`` (sonst waere ``/ 1000``
keine Ganzzahldivision mehr) und ``code character(2)`` beim Laendercode.
Ebenso die View ``release_event`` als Vereinigung von ``release_country``
und ``release_unknown_country`` — der Standardfall eines mit ``createdb.sh``
gebauten Spiegels.

Das Schema entsteht in **derselben** Wegwerf-Datenbank wie die
AcoustID-Tabellen; unsere liegen in ``public``, MusicBrainz in
``musicbrainz``. Dadurch braucht der Testlauf nur einen Postgres-Dienst.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

import psycopg

__all__ = [
    "MBID_MERGED",
    "MBID_NO_RELEASE",
    "MBID_ONE",
    "MBID_TWO",
    "RELEASE_GROUP_GID",
    "RELEASE_ONE_GID",
    "RELEASE_TWO_GID",
    "SCHEMA_SEQUENCE",
    "TRACK_ONE_GID",
    "create_schema",
    "seed",
]

#: Aufnahme mit zwei Veroeffentlichungen und einem Artist-Credit aus zwei
#: Kuenstlern (prueft ``join_phrase`` und die Batch-Aufloesung).
MBID_ONE: Final = "11111111-1111-1111-1111-111111111111"

#: Aufnahme auf dem zweiten Medium desselben Release.
MBID_TWO: Final = "22222222-2222-2222-2222-222222222222"

#: Aufnahme ohne jede Veroeffentlichung (Fallstrick 1).
MBID_NO_RELEASE: Final = "33333333-3333-3333-3333-333333333333"

#: Zusammengefuehrte Aufnahme: zeigt per ``recording_gid_redirect`` auf
#: :data:`MBID_ONE`.
MBID_MERGED: Final = "99999999-9999-9999-9999-999999999999"

RELEASE_ONE_GID: Final = "aaaa0001-0000-0000-0000-000000000000"
RELEASE_TWO_GID: Final = "aaaa0002-0000-0000-0000-000000000000"
RELEASE_GROUP_GID: Final = "bbbb0001-0000-0000-0000-000000000000"
TRACK_ONE_GID: Final = "cccc0001-0000-0000-0000-000000000000"

#: Schema-Sequenz, die der Client erwartet (`EXPECTED_SCHEMA_SEQUENCE`).
SCHEMA_SEQUENCE: Final = 31

#: Laenge der ersten Aufnahme in Millisekunden — bewusst knapp unter einer
#: vollen Sekunde, damit die Ganzzahldivision sichtbar wird (209, nicht 210).
LENGTH_ONE_MS: Final = 209_999

_DDL: Final = """
CREATE SCHEMA musicbrainz;

CREATE TABLE musicbrainz.artist (
    id          integer PRIMARY KEY,
    gid         uuid    NOT NULL,
    name        varchar NOT NULL,
    sort_name   varchar NOT NULL
);

CREATE TABLE musicbrainz.artist_credit (
    id          integer PRIMARY KEY,
    name        varchar NOT NULL
);

CREATE TABLE musicbrainz.artist_credit_name (
    artist_credit integer NOT NULL,
    position      smallint NOT NULL,
    artist        integer NOT NULL,
    name          varchar NOT NULL,
    join_phrase   text NOT NULL DEFAULT '',
    PRIMARY KEY (artist_credit, position)
);

CREATE TABLE musicbrainz.recording (
    id            integer PRIMARY KEY,
    gid           uuid    NOT NULL UNIQUE,
    name          varchar NOT NULL,
    artist_credit integer NOT NULL,
    length        integer,
    comment       varchar NOT NULL DEFAULT '',
    video         boolean NOT NULL DEFAULT false
);

CREATE TABLE musicbrainz.recording_gid_redirect (
    gid     uuid PRIMARY KEY,
    new_id  integer NOT NULL
);

CREATE TABLE musicbrainz.release_group_primary_type (
    id   integer PRIMARY KEY,
    name varchar NOT NULL
);

CREATE TABLE musicbrainz.release_group_secondary_type (
    id          integer PRIMARY KEY,
    name        varchar NOT NULL,
    child_order integer NOT NULL DEFAULT 0
);

CREATE TABLE musicbrainz.release_group (
    id            integer PRIMARY KEY,
    gid           uuid    NOT NULL,
    name          varchar NOT NULL,
    artist_credit integer NOT NULL,
    type          integer
);

CREATE TABLE musicbrainz.release_group_secondary_type_join (
    release_group  integer NOT NULL,
    secondary_type integer NOT NULL,
    PRIMARY KEY (release_group, secondary_type)
);

CREATE TABLE musicbrainz.release (
    id            integer PRIMARY KEY,
    gid           uuid    NOT NULL,
    name          varchar NOT NULL,
    artist_credit integer NOT NULL,
    release_group integer NOT NULL
);

CREATE TABLE musicbrainz.medium_format (
    id   integer PRIMARY KEY,
    name varchar NOT NULL
);

CREATE TABLE musicbrainz.medium (
    id          integer PRIMARY KEY,
    release     integer NOT NULL,
    position    integer NOT NULL,
    format      integer,
    name        varchar NOT NULL DEFAULT '',
    track_count integer NOT NULL DEFAULT 0
);

CREATE TABLE musicbrainz.track (
    id            integer PRIMARY KEY,
    gid           uuid    NOT NULL,
    recording     integer NOT NULL,
    medium        integer NOT NULL,
    position      integer NOT NULL,
    number        text    NOT NULL,
    name          varchar NOT NULL,
    artist_credit integer NOT NULL,
    length        integer,
    is_data_track boolean NOT NULL DEFAULT false
);
CREATE INDEX track_idx_recording ON musicbrainz.track (recording);

CREATE TABLE musicbrainz.release_country (
    release    integer NOT NULL,
    country    integer NOT NULL,
    date_year  smallint,
    date_month smallint,
    date_day   smallint,
    PRIMARY KEY (release, country)
);

CREATE TABLE musicbrainz.release_unknown_country (
    release    integer PRIMARY KEY,
    date_year  smallint,
    date_month smallint,
    date_day   smallint
);

CREATE TABLE musicbrainz.iso_3166_1 (
    area integer NOT NULL,
    code character(2) PRIMARY KEY
);

CREATE TABLE musicbrainz.replication_control (
    id                          integer PRIMARY KEY,
    current_schema_sequence     integer NOT NULL,
    current_replication_sequence integer,
    last_replication_date       timestamp with time zone
);

-- Die View, die musicbrainz-docker beim Anlegen der Datenbank erzeugt.
CREATE VIEW musicbrainz.release_event AS
    SELECT release, country, date_year, date_month, date_day
    FROM musicbrainz.release_country
  UNION ALL
    SELECT release, NULL::integer AS country, date_year, date_month, date_day
    FROM musicbrainz.release_unknown_country;
"""

# Je Anweisung ein Eintrag: psycopg schickt Anweisungen MIT Parametern ueber
# das erweiterte Protokoll, und das kennt nur eine Anweisung pro Aufruf.
_SEED: Final[tuple[str, ...]] = (
    """
    INSERT INTO musicbrainz.artist (id, gid, name, sort_name) VALUES
        (1, 'dddd0001-0000-0000-0000-000000000000', 'Beispielband', 'Beispielband'),
        (2, 'dddd0002-0000-0000-0000-000000000000', 'Gaststimme', 'Gaststimme')
    """,
    """
    INSERT INTO musicbrainz.artist_credit (id, name) VALUES
        (10, 'Beispielband feat. Gaststimme'),
        (11, 'Beispielband')
    """,
    # Zwei Glieder mit join_phrase: prueft die Anzeigereihenfolge und dass
    # eine leere Phrase nicht in die Antwort kommt.
    """
    INSERT INTO musicbrainz.artist_credit_name
        (artist_credit, position, artist, name, join_phrase) VALUES
        (10, 0, 1, 'Beispielband', ' feat. '),
        (10, 1, 2, 'Gaststimme', ''),
        (11, 0, 1, 'Beispielband', '')
    """,
    """
    INSERT INTO musicbrainz.recording (id, gid, name, artist_credit, length) VALUES
        (1, %(mbid_one)s,        'Erstes Stueck',  10, %(length_one)s),
        (2, %(mbid_two)s,        'Zweites Stueck', 11, 185000),
        (3, %(mbid_no_release)s, 'Ohne Album',     11, 60000)
    """,
    "INSERT INTO musicbrainz.recording_gid_redirect (gid, new_id) VALUES (%(mbid_merged)s, 1)",
    "INSERT INTO musicbrainz.release_group_primary_type (id, name) VALUES (1, 'Album')",
    """
    INSERT INTO musicbrainz.release_group_secondary_type (id, name, child_order) VALUES
        (1, 'Compilation', 1),
        (2, 'Live', 2)
    """,
    """
    INSERT INTO musicbrainz.release_group (id, gid, name, artist_credit, type) VALUES
        (700, %(release_group)s, 'Erstes Album', 11, 1)
    """,
    # Absichtlich in verkehrter Reihenfolge: die Abfrage sortiert nach child_order.
    """
    INSERT INTO musicbrainz.release_group_secondary_type_join
        (release_group, secondary_type) VALUES (700, 2), (700, 1)
    """,
    """
    INSERT INTO musicbrainz.release (id, gid, name, artist_credit, release_group) VALUES
        (500, %(release_one)s, 'Erstes Album', 11, 700),
        (501, %(release_two)s, 'Erstes Album (Neuauflage)', 11, 700)
    """,
    "INSERT INTO musicbrainz.medium_format (id, name) VALUES (1, 'CD')",
    """
    INSERT INTO musicbrainz.medium (id, release, position, format, name, track_count) VALUES
        (5000, 500, 1, 1,    '',         12),
        (5001, 500, 2, 1,    'Bonus-CD', 10),
        (5002, 501, 1, NULL, '',          8)
    """,
    # Aufnahme 1 steckt in beiden Veroeffentlichungen und im ersten Release
    # auf beiden Medien (Bonus-Track). Track 9002 hat keine Laenge — in
    # MusicBrainz nicht selten und im Original ein TypeError.
    """
    INSERT INTO musicbrainz.track
        (id, gid, recording, medium, position, number, name, artist_credit, length) VALUES
        (9000, %(track_one)s, 1, 5000, 4, '4', 'Erstes Stueck', 10, %(length_one)s),
        (9001, 'cccc0002-0000-0000-0000-000000000000', 2, 5001, 2, '2',
               'Zweites Stueck', 11, 185000),
        (9002, 'cccc0003-0000-0000-0000-000000000000', 1, 5002, 1, '1',
               'Erstes Stueck', 10, NULL),
        (9003, 'cccc0004-0000-0000-0000-000000000000', 1, 5001, 5, '5',
               'Erstes Stueck (Live)', 10, 215000)
    """,
    "INSERT INTO musicbrainz.iso_3166_1 (area, code) VALUES (13, 'DE')",
    """
    INSERT INTO musicbrainz.release_country (release, country, date_year, date_month, date_day)
        VALUES (500, 13, 1999, 7, NULL)
    """,
    # Zweite Veroeffentlichung ohne Land: nur ueber die View bzw. den
    # Rueckfallweg sichtbar (die Referenz-Implementierung sieht sie nie).
    """
    INSERT INTO musicbrainz.release_unknown_country (release, date_year, date_month, date_day)
        VALUES (501, 2004, NULL, NULL)
    """,
    """
    INSERT INTO musicbrainz.replication_control
        (id, current_schema_sequence, current_replication_sequence, last_replication_date)
        VALUES (1, %(schema_sequence)s, 4242, now() - interval '2 hours')
    """,
)

_PARAMS: Final = {
    "mbid_one": UUID(MBID_ONE),
    "mbid_two": UUID(MBID_TWO),
    "mbid_no_release": UUID(MBID_NO_RELEASE),
    "mbid_merged": UUID(MBID_MERGED),
    "release_group": UUID(RELEASE_GROUP_GID),
    "release_one": UUID(RELEASE_ONE_GID),
    "release_two": UUID(RELEASE_TWO_GID),
    "track_one": UUID(TRACK_ONE_GID),
    "length_one": LENGTH_ONE_MS,
    "schema_sequence": SCHEMA_SEQUENCE,
}


def create_schema(connection: psycopg.Connection) -> None:
    """Legt Schema, Tabellen und die ``release_event``-View an."""
    connection.execute(_DDL)


def seed(connection: psycopg.Connection) -> None:
    """Spielt den synthetischen Bestand ein (siehe Modul-Docstring)."""
    for statement in _SEED:
        connection.execute(statement, _PARAMS)
