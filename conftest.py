"""Zentrale pytest-Konfiguration: Steuerung der Integrationstests.

Tests mit dem Marker ``integration`` brauchen einen echten Dienst aus dem
Compose-Stack. Welchen, sagen weitere Marker:

=================================  =======================================
nur ``integration``                Postgres (Compose-Service `db`) — der
                                   Vorgabefall seit Phase 4.
``integration`` + ``index``        acoustid-index (Compose-Service `index`,
                                   ``AOFF_INDEX_URL``) — seit Phase 5.
``integration`` + ``db`` +         beide zugleich — der Index-Feed liest aus
``index``                          der Postgres und schreibt in den Index
                                   (Phase 7). Der Marker ``db`` ist nur
                                   noetig, wenn ``index`` mit dabei ist.
=================================  =======================================

Ob sie laufen, entscheidet fuer jeden Dienst einzeln derselbe Schalter:

===========================  =============================================
``--integration=auto``       Default: die Verbindung wird einmal je Dienst
                             geprobt; ist sie da, laufen die Tests, sonst
                             werden sie **mit Begruendung im Report**
                             abgewaehlt.
``--integration=require``    Erzwingt sie — ist ein gebrauchter Dienst nicht
                             erreichbar, scheitert der Lauf (so laeuft die CI).
``--integration=off``        Immer abwaehlen, ebenfalls mit Begruendung.
===========================  =============================================

Alternativ ueber die Umgebungsvariable ``ACOUSTID_INTEGRATION_TESTS``
(gleiche Werte). Bewusst ohne `AOFF_`-Praefix: das sind Bootstrap-Variablen
der Anwendung, deren Satz gegen `.env.example` geprueft wird.

Davon unabhaengig gibt es den Marker ``network`` (seit Phase 6): Tests, die
das **echte** Internet brauchen (data.acoustid.org). Sie sind per Vorgabe
abgewaehlt — mit Begruendung im Report — und laufen nur mit ``--network``
bzw. ``ACOUSTID_NETWORK_TESTS=1``; in der CI laufen sie nie.

Die Zugaenge kommen aus denselben `AOFF_`-Variablen wie im Betrieb
(`shared.env.EnvSettings`); fuer den lokalen Lauf gegen Compose siehe
`tests/docker-compose.test.yml`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# pytest legt wegen dieser Datei das Repo-Wurzelverzeichnis auf sys.path.
# Dort liegt das Workspace-Member-Verzeichnis `shared/`, das als
# Namespace-Paket den Editable-Import des echten Pakets `shared` verdeckt
# (LEARNINGS: "Mehrere gleichnamige Python-Pakete kollidieren im venv").
# Deshalb den Eintrag hier wieder entfernen, bevor Tests importiert werden.
_REPO_ROOT = str(Path(__file__).resolve().parent)
sys.path[:] = [entry for entry in sys.path if entry not in ("", _REPO_ROOT)]
if getattr(sys.modules.get("shared"), "__file__", "") is None:
    del sys.modules["shared"]

import pytest  # noqa: E402  (erst nach der sys.path-Korrektur importieren)

MARKER = "integration"
DB_MARKER = "db"
INDEX_MARKER = "index"
NETWORK_MARKER = "network"
ENV_SWITCH = "ACOUSTID_INTEGRATION_TESTS"
NETWORK_SWITCH = "ACOUSTID_NETWORK_TESTS"
MODES = ("auto", "require", "off")

#: Menschenlesbare Namen der Dienste (fuer Meldungen im Report).
SERVICES = {DB_MARKER: "Postgres", INDEX_MARKER: "acoustid-index"}

_STATUS_KEY = pytest.StashKey[str]()
_NETWORK_KEY = pytest.StashKey[str]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration",
        choices=MODES,
        default=os.environ.get(ENV_SWITCH, "auto"),
        help=(
            "Integrationstests: auto (Default, laufen wenn der jeweilige Dienst "
            f"erreichbar ist), require (erzwingen) oder off. Auch ueber {ENV_SWITCH} setzbar."
        ),
    )
    parser.addoption(
        "--network",
        action="store_true",
        default=os.environ.get(NETWORK_SWITCH, "").strip().lower() in ("1", "true", "yes"),
        help=(
            f"Tests mit dem Marker '{NETWORK_MARKER}' mitlaufen lassen (echtes Netz, "
            f"data.acoustid.org). Auch ueber {NETWORK_SWITCH}=1 setzbar."
        ),
    )


def _probe_db() -> tuple[bool, str]:
    """Prueft einmalig, ob die konfigurierte Postgres erreichbar ist."""
    import psycopg

    from shared.env import EnvError, EnvSettings

    try:
        dsn = EnvSettings.from_env().db_dsn().get_secret_value()
    except EnvError as error:
        return False, str(error)
    try:
        with psycopg.connect(dsn, connect_timeout=5):
            pass
    except psycopg.OperationalError as error:
        return False, f"Postgres nicht erreichbar: {str(error).strip()}"
    return True, "Postgres erreichbar"


def _probe_index() -> tuple[bool, str]:
    """Prueft einmalig, ob der acoustid-index antwortet.

    Bewusst gegen ``/_health`` (Lebendpruefung): ob der *Index* schon
    existiert, ist Sache der Tests — sie legen ihn selbst an.
    """
    import httpx

    from shared.env import EnvError, EnvSettings

    try:
        url = EnvSettings.from_env().index_url.rstrip("/")
    except EnvError as error:
        return False, str(error)
    try:
        response = httpx.get(f"{url}/_health", timeout=5)
    except httpx.HTTPError as error:
        return False, f"acoustid-index nicht erreichbar unter {url}: {error}"
    if response.status_code != 200:
        return False, f"acoustid-index unter {url} antwortet mit HTTP {response.status_code}"
    return True, f"acoustid-index erreichbar unter {url}"


_PROBES = {DB_MARKER: _probe_db, INDEX_MARKER: _probe_index}


def _required_services(item: pytest.Item) -> tuple[str, ...]:
    """Welche Dienste braucht dieser Test?

    Ohne weiteren Marker: Postgres (Vorgabefall seit Phase 4). Der Marker
    ``index`` schaltet auf den acoustid-index um; wer beides braucht, setzt
    ``db`` ausdruecklich dazu.
    """
    if not item.get_closest_marker(INDEX_MARKER):
        return (DB_MARKER,)
    if item.get_closest_marker(DB_MARKER):
        return (DB_MARKER, INDEX_MARKER)
    return (INDEX_MARKER,)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # trylast: erst greift pytests eigene `-m`-Auswahl, dann diese. Sonst
    # wuerden wir Dienste anproben, die durch `-m` ohnehin schon draussen
    # sind (z. B. `-m "integration and index"` ohne Postgres).
    _select_network_tests(config, items)

    mode = config.getoption("--integration")
    marked = [item for item in items if item.get_closest_marker(MARKER)]
    if not marked:
        config.stash[_STATUS_KEY] = f"Modus {mode}, keine Integrationstests gesammelt"
        return

    by_service: dict[str, list[pytest.Item]] = {}
    for item in marked:
        for service in _required_services(item):
            by_service.setdefault(service, []).append(item)

    notes: list[str] = []
    # Dienst -> Begruendung. Ein Test faellt raus, sobald einer der Dienste
    # fehlt, die er braucht — deshalb erst alle proben, dann auswaehlen.
    unavailable: dict[str, str] = {}
    for service in sorted(by_service):
        group = by_service[service]
        label = SERVICES[service]
        if mode == "off":
            notes.append(f"{len(group)}x {label} abgewaehlt (--integration=off)")
            unavailable[service] = "--integration=off"
            continue
        reachable, detail = _PROBES[service]()
        if reachable:
            notes.append(f"{len(group)}x {label} laufen — {detail}")
            continue
        if mode == "require":
            # Kein stilles Ueberspringen: mit require ist das ein Fehler.
            raise pytest.UsageError(
                f"--integration=require, aber {detail}. Erwartet werden die "
                f"AOFF_-Variablen eines erreichbaren Dienstes ({label})."
            )
        notes.append(f"{len(group)}x {label} abgewaehlt — {detail}")
        unavailable[service] = detail

    config.stash[_STATUS_KEY] = "; ".join(notes)
    if unavailable:
        deselected = [
            item
            for item in marked
            if any(service in unavailable for service in _required_services(item))
        ]
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item not in deselected]


def _select_network_tests(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Netz-Tests abwaehlen, solange ``--network`` nicht gesetzt ist."""
    if config.getoption("--network"):
        return
    marked = [item for item in items if item.get_closest_marker(NETWORK_MARKER)]
    if not marked:
        return
    config.stash[_NETWORK_KEY] = (
        f"{len(marked)}x abgewaehlt (echtes Netz; mit --network bzw. {NETWORK_SWITCH}=1 laufen sie)"
    )
    config.hook.pytest_deselected(items=marked)
    items[:] = [item for item in items if item not in marked]


def pytest_report_header(config: pytest.Config) -> str:
    network = "an" if config.getoption("--network") else "aus"
    return f"integration: Modus {config.getoption('--integration')}; network: {network}"


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    status = terminalreporter.config.stash.get(_STATUS_KEY, None)
    if status is not None:
        terminalreporter.write_line(f"integration: {status}")
    network = terminalreporter.config.stash.get(_NETWORK_KEY, None)
    if network is not None:
        terminalreporter.write_line(f"network: {network}")
