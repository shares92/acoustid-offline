"""Zentrale pytest-Konfiguration: Steuerung der Integrationstests.

Tests mit dem Marker ``integration`` brauchen eine echte Postgres (den
Compose-Service `db`). Ob sie laufen, entscheidet dieser Schalter:

===========================  =============================================
``--integration=auto``       Default: Verbindung wird einmal geprobt; ist
                             sie da, laufen die Tests, sonst werden sie
                             **mit Begruendung im Report** abgewaehlt.
``--integration=require``    Erzwingt sie — ist keine DB erreichbar,
                             scheitert der Lauf (so laeuft die CI).
``--integration=off``        Immer abwaehlen, ebenfalls mit Begruendung.
===========================  =============================================

Alternativ ueber die Umgebungsvariable ``ACOUSTID_INTEGRATION_TESTS``
(gleiche Werte). Bewusst ohne `AOFF_`-Praefix: das sind Bootstrap-Variablen
der Anwendung, deren Satz gegen `.env.example` geprueft wird.

Der Zugang kommt aus denselben `AOFF_DB_*`-Variablen wie im Betrieb
(`shared.env.EnvSettings.db_dsn`); fuer den lokalen Lauf gegen Compose
siehe `tests/docker-compose.test.yml`.
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
ENV_SWITCH = "ACOUSTID_INTEGRATION_TESTS"
MODES = ("auto", "require", "off")

_STATUS_KEY = pytest.StashKey[str]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration",
        choices=MODES,
        default=os.environ.get(ENV_SWITCH, "auto"),
        help=(
            "Integrationstests: auto (Default, laufen wenn Postgres erreichbar), "
            f"require (erzwingen) oder off. Auch ueber {ENV_SWITCH} setzbar."
        ),
    )


def _probe() -> tuple[bool, str]:
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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    mode = config.getoption("--integration")
    marked = [item for item in items if item.get_closest_marker(MARKER)]
    if not marked:
        config.stash[_STATUS_KEY] = f"Modus {mode}, keine Integrationstests gesammelt"
        return

    if mode == "off":
        reason = "abgewaehlt (--integration=off)"
    else:
        reachable, detail = _probe()
        if reachable:
            config.stash[_STATUS_KEY] = f"{len(marked)} Test(s) laufen — {detail}"
            return
        if mode == "require":
            # Kein stilles Ueberspringen: mit require ist das ein Fehler.
            raise pytest.UsageError(
                f"--integration=require, aber {detail}. Erwartet werden die "
                "AOFF_DB_*-Variablen einer erreichbaren Postgres."
            )
        reason = f"abgewaehlt — {detail}"

    config.stash[_STATUS_KEY] = f"{len(marked)} Test(s) {reason}"
    config.hook.pytest_deselected(items=marked)
    items[:] = [item for item in items if item not in marked]


def pytest_report_header(config: pytest.Config) -> str:
    return f"integration: Modus {config.getoption('--integration')}"


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    status = terminalreporter.config.stash.get(_STATUS_KEY, None)
    if status is not None:
        terminalreporter.write_line(f"integration: {status}")
