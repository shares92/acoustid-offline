#!/bin/sh
# Vorlauf des Ein-Container-Betriebs (HANDOFF v2 §3, DECISIONS 2026-08-04 E1/E14/E16).
#
# Laeuft als Kind von `tini` (PID 1), bevor supervisord uebernimmt, und macht
# genau die Dinge, die *einmal* passieren muessen und die kein Dauerdienst
# selbst tun kann:
#
#   1. Verzeichnisse der sechs Mounts anlegen und den Eigentuemer setzen.
#   2. Postgres-Cluster anlegen, wenn `/data/db/<major>/` leer ist (initdb),
#      inkl. Rolle und Datenbank der Anwendung.
#   3. Das interne Datenbank-Passwort erzeugen (E16: kein `.env`-Pflichtwert
#      mehr) und an die Kindprozesse durchreichen.
#   4. Auf einen Versions-Drift hinweisen (der *harte* Guard sitzt im
#      Waechter, E14 — hier wird nur gewarnt: ein Abbruch nähme dem Betreiber
#      auch die Admin-UI, mit der er den Fehler sehen soll).
#
# Danach `exec "$@"` — supervisord erbt PID-Weitergabe und Umgebung.
#
# Bewusst POSIX-sh und ohne Python: dieser Vorlauf muss auch dann noch
# funktionieren, wenn die Anwendung selbst nicht startet.
set -eu

log() {
    printf '%s entrypoint: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

die() {
    log "FEHLER: $*"
    exit 1
}

# --- Umgebung ---------------------------------------------------------------

# Bootstrap-Werte der Anwendung (shared/shared/env.py). Hier werden nur die
# gebraucht, die auch das Dateisystem betreffen.
AOFF_DATA_DIR="${AOFF_DATA_DIR:-/config}"
AOFF_DUMP_DIR="${AOFF_DUMP_DIR:-/import}"
AOFF_DB_DATA_ROOT="${AOFF_DB_DATA_ROOT:-/data/db}"
AOFF_PG_MAJOR="${AOFF_PG_MAJOR:-18}"
AOFF_DB_NAME="${AOFF_DB_NAME:-acoustid}"
AOFF_DB_USER="${AOFF_DB_USER:-acoustid}"
AOFF_DB_PORT="${AOFF_DB_PORT:-5432}"

# Container-Werte ohne `AOFF_`-Praefix: sie gehoeren nicht ins Schema der
# Anwendung, und `EnvSettings` wuerde sie als unbekannt melden (Muster aus
# docker-compose.yml v1: ACOUSTID_WATCHDOG_DATA).
ACOUSTID_INDEX_DIR="${ACOUSTID_INDEX_DIR:-/index}"
ACOUSTID_BACKUP_DIR="${ACOUSTID_BACKUP_DIR:-/backup}"

PGBIN="/usr/lib/postgresql/${AOFF_PG_MAJOR}/bin"
PGDATA="${AOFF_DB_DATA_ROOT}/${AOFF_PG_MAJOR}"
PASSWORD_FILE="${AOFF_DATA_DIR}/db-password"

export AOFF_DATA_DIR AOFF_DUMP_DIR AOFF_DB_DATA_ROOT AOFF_PG_MAJOR
export AOFF_DB_NAME AOFF_DB_USER AOFF_DB_PORT
export ACOUSTID_INDEX_DIR ACOUSTID_BACKUP_DIR PGDATA

# --- 1. Verzeichnisse -------------------------------------------------------
#
# `/backup` wird nur angelegt, wenn der Mount da ist: der Backup-Job kommt
# erst in M2.5 (K9), das Verzeichnis soll aber ohne Image-Wechsel nutzbar sein.

mkdir -p "${AOFF_DATA_DIR}/logs" "${AOFF_DUMP_DIR}" "${AOFF_DB_DATA_ROOT}" "${ACOUSTID_INDEX_DIR}"
chmod 0755 "${AOFF_DATA_DIR}" "${AOFF_DB_DATA_ROOT}"
# Der Index laeuft unter der UID des Upstream-Images (6081) — dieselbe UID
# wie in v1, damit ein bestehendes Index-Verzeichnis ohne chown weiterlaeuft.
chown acoustid:acoustid "${ACOUSTID_INDEX_DIR}"
chown postgres:postgres "${AOFF_DB_DATA_ROOT}"

# --- 2./3. Postgres-Cluster und internes Passwort ---------------------------

# Ein von aussen gesetztes Passwort gewinnt: so laesst sich ein Bestand aus
# v1 uebernehmen (docs/migration-v1-v2.md §7) und die Test-Zusammenstellung
# kennt den Zugang. Ohne Angabe erzeugt der Entrypoint eines — der
# `.env`-Pflichtwert von v1 entfaellt damit (E16).
if [ -n "${AOFF_DB_PASSWORD:-}" ]; then
    (
        umask 0077
        printf '%s' "${AOFF_DB_PASSWORD}" > "${PASSWORD_FILE}"
    )
    log "Datenbank-Passwort aus der Umgebung uebernommen"
elif [ ! -s "${PASSWORD_FILE}" ]; then
    # 24 Byte aus /dev/urandom, base64 ohne Sonderzeichen im DSN.
    (
        umask 0077
        head -c 24 /dev/urandom | base64 | tr -d '\n=+/' > "${PASSWORD_FILE}"
    )
    log "internes Datenbank-Passwort erzeugt (${PASSWORD_FILE})"
fi
chmod 0600 "${PASSWORD_FILE}"
AOFF_DB_PASSWORD="$(cat "${PASSWORD_FILE}")"
export AOFF_DB_PASSWORD

[ -x "${PGBIN}/initdb" ] || die "Postgres ${AOFF_PG_MAJOR} fehlt im Image (${PGBIN})"

if [ ! -s "${PGDATA}/PG_VERSION" ]; then
    if [ -d "${PGDATA}" ] && [ -n "$(ls -A "${PGDATA}" 2>/dev/null || true)" ]; then
        die "${PGDATA} ist nicht leer, enthaelt aber kein Cluster — Migrationsrezept pruefen (docs/migration-v1-v2.md)"
    fi
    log "leeres Datenverzeichnis — initdb fuer Postgres ${AOFF_PG_MAJOR} in ${PGDATA}"
    mkdir -p "${PGDATA}"
    chown postgres:postgres "${PGDATA}"
    chmod 0700 "${PGDATA}"
    # Encoding und Locale werden festgenagelt: ein spaeterer Wechsel der
    # Wirts-Locale darf die Sortierung eines 200-GB-Bestands nicht aendern
    # (Reindex-Pflicht). C.UTF-8 ist in jedem Debian ohne locales-Paket da.
    gosu postgres "${PGBIN}/initdb" \
        --pgdata="${PGDATA}" \
        --username=postgres \
        --encoding=UTF8 \
        --locale=C.UTF-8 \
        --auth-local=trust \
        --auth-host=scram-sha-256 >&2

    # Adresse und Port setzt der Wrapper `mmo-postgres` auf der Kommandozeile
    # (eine Quelle statt zweier); postgresql.conf bleibt so, wie initdb sie
    # geschrieben hat.
    #
    # pg_hba: initdb traegt nur Loopback-Zeilen ein. Der Torwaechter ist
    # aber `listen_addresses` (im Betrieb 127.0.0.1) — eine Verbindung von
    # aussen kommt gar nicht erst an. Nur die Test-Zusammenstellung
    # veroeffentlicht den Port, und dann kommt die Verbindung ueber das
    # Docker-Gateway und nicht ueber das Loopback-Interface. Passwort bleibt
    # in jedem Fall Pflicht (scram-sha-256).
    echo "host all all all scram-sha-256" >> "${PGDATA}/pg_hba.conf"

    log "Rolle und Datenbank anlegen"
    gosu postgres "${PGBIN}/pg_ctl" -D "${PGDATA}" -w -o "-c listen_addresses=''" start >&2
    # In v1 war die Anwendungsrolle **Superuser** — das offizielle
    # Postgres-Image macht das stillschweigend mit `POSTGRES_USER`. Hier
    # wird sie es nicht; stattdessen genau die zwei Rechte, die im Bestand
    # wirklich gebraucht werden:
    #
    #   CREATEDB       Wegwerf-Datenbanken der Integrationstests
    #   pg_checkpoint  `CHECKPOINT` am Ende des Bulk-Imports
    #                  (`importer/app/bulk.py`, `flush_wal`) — sonst faellt
    #                  der auf seinen gutartigen Ausweichpfad zurueck und
    #                  der Bootstrap steht nach Stunden auf ungesichertem
    #                  Grund, ohne dass es jemand merkt.
    gosu postgres "${PGBIN}/psql" --quiet --no-psqlrc --set ON_ERROR_STOP=1 \
        --dbname=postgres \
        --command="CREATE ROLE \"${AOFF_DB_USER}\" LOGIN CREATEDB PASSWORD '${AOFF_DB_PASSWORD}'" \
        --command="GRANT pg_checkpoint TO \"${AOFF_DB_USER}\"" >&2
    gosu postgres "${PGBIN}/createdb" --owner="${AOFF_DB_USER}" "${AOFF_DB_NAME}" >&2
    gosu postgres "${PGBIN}/pg_ctl" -D "${PGDATA}" -w -m fast stop >&2
    log "Cluster angelegt"
else
    # Das Passwort kann sich geaendert haben (geloeschte Datei, Restore) —
    # die Rolle wird beim naechsten Start nachgezogen, sobald die Datenbank
    # laeuft. Das erledigt der Waechter nicht: hier genuegt ein Hinweis.
    log "vorhandenes Cluster in ${PGDATA} (PG_VERSION $(cat "${PGDATA}/PG_VERSION"))"
fi

# --- 4. Versions-Drift (Hinweis; der Guard sitzt im Waechter, E14) ----------

for candidate in "${AOFF_DB_DATA_ROOT}"/*; do
    [ -d "${candidate}" ] || continue
    found="$(basename "${candidate}")"
    [ "${found}" = "${AOFF_PG_MAJOR}" ] && continue
    [ -s "${candidate}/PG_VERSION" ] || continue
    log "WARNUNG: Bestand einer anderen Major-Version gefunden (${candidate}) —" \
        "dieses Image bringt Postgres ${AOFF_PG_MAJOR} mit; siehe docs/migration-v1-v2.md"
done

log "Vorlauf fertig, uebergebe an: $*"
exec "$@"
