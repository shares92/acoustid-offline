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

# Uebergangslesen `AOFF_` -> `MMO_` (M2, eine Release-Runde; E5). Die
# Anwendung tut dasselbe in `shared/shared/env.py` — und **genau deshalb**
# muss dieser Vorlauf mitziehen: liefe hier der Vorgabewert, waehrend die
# Anwendung den alten Wert liest, bereitete der Entrypoint `/config` vor,
# waehrend Waechter und API in ein voellig anderes Verzeichnis schrieben.
# Ein halb umgezogener Container ist schlimmer als ein klar abgelehnter.
#
# `MMO_` gewinnt immer; uebernommen wird nur, was leer oder ungesetzt ist.
#
# **Genau eine Meldung je Altvariable.** Der Altname wird nach der
# Auswertung geloescht — sonst saehe ihn `shared/shared/env.py` in jedem
# Kindprozess erneut und meldete dieselbe Sache ein zweites Mal, im
# uebernommenen Fall sogar mit der falschen Aussage „wird ignoriert",
# obwohl der Wert gerade wirkt. Umgekehrt blieb der Fall „beide gesetzt"
# hier bisher voellig stumm: der Entrypoint uebersprang ihn, und env.py
# bekam den Altnamen wegen des `unset` nie zu sehen.
#
# Nur die Variablen dieser Liste werden hier verbraucht; alle uebrigen
# (PORT, LOG_LEVEL, API_*, INDEX_NAME) reicht der Vorlauf unangetastet
# weiter, damit env.py sie sieht und dort genau einmal warnt.
for name in DATA_DIR DUMP_DIR DB_DATA_ROOT PG_MAJOR DB_NAME DB_USER DB_PORT \
            DB_PASSWORD DB_PASSWORD_FILE; do
    eval "current=\${MMO_${name}:-}"
    eval "previous=\${AOFF_${name}:-}"
    [ -n "${previous}" ] || continue
    if [ -z "${current}" ]; then
        eval "MMO_${name}=\${AOFF_${name}}"
        eval "export MMO_${name}"
        log "veraltete Variable AOFF_${name} wird noch gelesen — bitte in MMO_${name} umbenennen"
    else
        log "veraltete Variable AOFF_${name} wird ignoriert, MMO_${name} ist gesetzt"
    fi
    eval "unset AOFF_${name}"
done
unset name current previous

# Bootstrap-Werte der Anwendung (shared/shared/env.py). Hier werden nur die
# gebraucht, die auch das Dateisystem betreffen.
MMO_DATA_DIR="${MMO_DATA_DIR:-/config}"
MMO_DUMP_DIR="${MMO_DUMP_DIR:-/import}"
MMO_DB_DATA_ROOT="${MMO_DB_DATA_ROOT:-/data/db}"
MMO_PG_MAJOR="${MMO_PG_MAJOR:-18}"
MMO_DB_NAME="${MMO_DB_NAME:-acoustid}"
MMO_DB_USER="${MMO_DB_USER:-acoustid}"
MMO_DB_PORT="${MMO_DB_PORT:-5432}"

# Container-Werte ohne `MMO_`-Praefix: sie gehoeren nicht ins Schema der
# Anwendung, und `EnvSettings` wuerde sie als unbekannt melden (Muster aus
# docker-compose.yml v1: ACOUSTID_WATCHDOG_DATA).
ACOUSTID_INDEX_DIR="${ACOUSTID_INDEX_DIR:-/index}"
ACOUSTID_BACKUP_DIR="${ACOUSTID_BACKUP_DIR:-/backup}"

# Die Datei mit dem Datenbank-Passwort. Der Vorgabewert ist derselbe, den
# `EnvSettings.db_password_file` ableitet (<data_dir>/db-password) — ein
# gesetztes MMO_DB_PASSWORD_FILE gewinnt aber, sonst waeren Docker-Secrets
# wirkungslos: die Anwendung laese dann eine andere Datei als die, die
# dieser Vorlauf beschreibt.
MMO_DB_PASSWORD_FILE="${MMO_DB_PASSWORD_FILE:-${MMO_DATA_DIR}/db-password}"

PGBIN="/usr/lib/postgresql/${MMO_PG_MAJOR}/bin"
PGDATA="${MMO_DB_DATA_ROOT}/${MMO_PG_MAJOR}"

export MMO_DATA_DIR MMO_DUMP_DIR MMO_DB_DATA_ROOT MMO_PG_MAJOR
export MMO_DB_NAME MMO_DB_USER MMO_DB_PORT MMO_DB_PASSWORD_FILE
export ACOUSTID_INDEX_DIR ACOUSTID_BACKUP_DIR PGDATA

# --- 1. Verzeichnisse -------------------------------------------------------
#
# `/backup` wird nur angelegt, wenn der Mount da ist: der Backup-Job kommt
# erst in M2.5 (K9), das Verzeichnis soll aber ohne Image-Wechsel nutzbar sein.

mkdir -p "${MMO_DATA_DIR}/logs" "${MMO_DUMP_DIR}" "${MMO_DB_DATA_ROOT}" "${ACOUSTID_INDEX_DIR}"

# /config gehoert root und der Gruppe `musicmeta` — und traegt das
# **setgid**-Bit (2750). Damit erben alle Dateien darin diese Gruppe, auch
# die, die der Waechter spaeter per tmp+rename schreibt (config.yaml). Das
# ist der Mechanismus, mit dem der unprivilegierte API-Dienst genau seine
# zwei Dateien lesen kann, ohne dass irgendwer sonst hineinsieht:
# `postgres` und `acoustid` kommen nicht einmal ins Verzeichnis.
chown root:musicmeta "${MMO_DATA_DIR}"
chmod 2750 "${MMO_DATA_DIR}"
chmod 0755 "${MMO_DB_DATA_ROOT}"
# Der Index laeuft unter der UID des Upstream-Images (6081) — dieselbe UID
# wie in v1, damit ein bestehendes Index-Verzeichnis ohne chown weiterlaeuft.
chown acoustid:acoustid "${ACOUSTID_INDEX_DIR}"
chown postgres:postgres "${MMO_DB_DATA_ROOT}"

# --- 2./3. Postgres-Cluster und internes Passwort ---------------------------

# Ein von aussen gesetztes Passwort gewinnt: so laesst sich ein Bestand aus
# v1 uebernehmen (docs/migration-v1-v2.md §7) und die Test-Zusammenstellung
# kennt den Zugang. Ohne Angabe erzeugt der Entrypoint eines — der
# `.env`-Pflichtwert von v1 entfaellt damit (E16).
mkdir -p "$(dirname "${MMO_DB_PASSWORD_FILE}")"
if [ -n "${MMO_DB_PASSWORD:-}" ]; then
    (
        umask 0077
        printf '%s' "${MMO_DB_PASSWORD}" > "${MMO_DB_PASSWORD_FILE}"
    )
    log "Datenbank-Passwort aus der Umgebung uebernommen"
elif [ ! -s "${MMO_DB_PASSWORD_FILE}" ]; then
    # 24 Byte aus /dev/urandom, base64 ohne Sonderzeichen im DSN.
    (
        umask 0077
        head -c 24 /dev/urandom | base64 | tr -d '\n=+/' > "${MMO_DB_PASSWORD_FILE}"
    )
    log "internes Datenbank-Passwort erzeugt (${MMO_DB_PASSWORD_FILE})"
fi
# Lesbar fuer root (Waechter, Importer) und die Gruppe `musicmeta` (der
# API-Dienst) — sonst niemand. Die Gruppe erbt die Datei ueber das
# setgid-Bit auf /config; bei einem abweichenden Pfad (Docker-Secret) wird
# sie hier ausdruecklich gesetzt.
chgrp musicmeta "${MMO_DB_PASSWORD_FILE}" 2>/dev/null || true
chmod 0640 "${MMO_DB_PASSWORD_FILE}"

# Bestandsdateien nachziehen: eine `config.yaml`, die ein aelterer Stand
# (oder v1) mit 0600 root:root geschrieben hat, koennte der unprivilegierte
# API-Dienst nicht lesen — und zwar bis zum naechsten Speichern in der
# Admin-UI. Das ist genau der Migrationsfall, also hier einmal richtigstellen.
if [ -f "${MMO_DATA_DIR}/config.yaml" ]; then
    chgrp musicmeta "${MMO_DATA_DIR}/config.yaml" 2>/dev/null || true
    chmod 0640 "${MMO_DATA_DIR}/config.yaml"
fi

# **Der Klartext wird NICHT exportiert.** supervisord vererbt seine
# Umgebung an jedes Kind — auch an das fremde `fpindex`, das mit der
# Datenbank nichts zu tun hat. Weitergereicht wird deshalb nur der
# Dateiname; API und Importer lesen ihn ueber `EnvSettings.db_password_file`
# (shared/shared/env.py), und wer per `docker compose exec` dazukommt,
# findet denselben Weg vor.
#
# Der alte Name muss **mit** verschwinden: waehrend des M2-Uebergangs steht
# das Passwort unter Umstaenden noch in `AOFF_DB_PASSWORD`, und die haette
# derselbe Export an dieselben Kinder weitergereicht — die Massnahme waere
# genau fuer die Bestandsinstallationen wirkungslos, fuer die sie gedacht ist.
unset MMO_DB_PASSWORD AOFF_DB_PASSWORD

[ -x "${PGBIN}/initdb" ] || die "Postgres ${MMO_PG_MAJOR} fehlt im Image (${PGBIN})"

if [ ! -s "${PGDATA}/PG_VERSION" ]; then
    if [ -d "${PGDATA}" ] && [ -n "$(ls -A "${PGDATA}" 2>/dev/null || true)" ]; then
        die "${PGDATA} ist nicht leer, enthaelt aber kein Cluster — Migrationsrezept pruefen (docs/migration-v1-v2.md)"
    fi
    log "leeres Datenverzeichnis — initdb fuer Postgres ${MMO_PG_MAJOR} in ${PGDATA}"
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
    #
    # Das Passwort kommt hier aus der Datei und geht ueber **stdin** in
    # psql — nicht als Argument: Argumente stehen in /proc und waeren fuer
    # jeden im Container sichtbar (`ps`), auch fuer die unprivilegierten
    # Dienste. Die Variable bleibt lokal und wird gleich wieder vergessen.
    role_password="$(cat "${MMO_DB_PASSWORD_FILE}")"
    printf "CREATE ROLE %s LOGIN CREATEDB PASSWORD '%s';\nGRANT pg_checkpoint TO %s;\n" \
        "\"${MMO_DB_USER}\"" "${role_password}" "\"${MMO_DB_USER}\"" \
        | gosu postgres "${PGBIN}/psql" --quiet --no-psqlrc --set ON_ERROR_STOP=1 \
            --dbname=postgres >&2
    unset role_password
    gosu postgres "${PGBIN}/createdb" --owner="${MMO_DB_USER}" "${MMO_DB_NAME}" >&2
    gosu postgres "${PGBIN}/pg_ctl" -D "${PGDATA}" -w -m fast stop >&2
    log "Cluster angelegt"
else
    # Das Passwort kann sich geaendert haben (geloeschte Datei, Restore) —
    # die Rolle wird beim naechsten Start nachgezogen, sobald die Datenbank
    # laeuft. Das erledigt der Waechter nicht: hier genuegt ein Hinweis.
    log "vorhandenes Cluster in ${PGDATA} (PG_VERSION $(cat "${PGDATA}/PG_VERSION"))"
fi

# --- 4. Versions-Drift (Hinweis; der Guard sitzt im Waechter, E14) ----------

for candidate in "${MMO_DB_DATA_ROOT}"/*; do
    [ -d "${candidate}" ] || continue
    found="$(basename "${candidate}")"
    [ "${found}" = "${MMO_PG_MAJOR}" ] && continue
    [ -s "${candidate}/PG_VERSION" ] || continue
    log "WARNUNG: Bestand einer anderen Major-Version gefunden (${candidate}) —" \
        "dieses Image bringt Postgres ${MMO_PG_MAJOR} mit; siehe docs/migration-v1-v2.md"
done

log "Vorlauf fertig, uebergebe an: $*"
exec "$@"
