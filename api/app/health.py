"""Interner Healthcheck des API-Dienstes (DECISIONS 2026-08-01).

Die Bereitschaftspruefung des Wake-on-request: der Waechter fragt hier, ob
der gerade gestartete Stack Anfragen beantworten kann, und laesst die
gehaltene Client-Anfrage erst dann durch
(:mod:`acoustid_watchdog.wake`).

**Warum ein eigener Endpunkt.** „Der Prozess lauscht" ist nicht dasselbe
wie „die Backends sind da": nach einem Kaltstart antwortet uvicorn lange
bevor die Postgres ihre Recovery beendet oder der Index seine 40-55 GB
gelesen hat. Ein TCP-Connect wuerde das nicht sehen, und eine bestehende
Route (etwa eine definierte Fehlerantwort von ``/v2/lookup``) als Sonde zu
missbrauchen, koppelt die Weck-Logik an Verhalten, das sich aus ganz
anderen Gruenden aendern darf.

**Warum er nicht im Vertrag steht.** ARCHITECTURE §7 beschreibt die
oeffentliche API; dieser Endpunkt gehoert nicht dazu. Er liegt deshalb
bewusst **nicht** unter ``/v2/``, antwortet **nicht** im
AcoustID-Fehlerformat (dessen 19 Codes passen auf nichts hier) und taucht
in keiner Client-Dokumentation auf. Erreichbar ist er ohnehin nur im
Compose-Netz: der Dienst hat keinen veroeffentlichten Port, und der Proxy
des Waechters reicht nur ``/v2/*`` weiter.

**Was geprueft wird.** Genau die zwei Anbindungen, ohne die kein Lookup
zustande kommt — und beide so leichtgewichtig wie moeglich, weil der
Waechter im Sekundentakt fragt:

==========  ==========================================================
``db``      ``SELECT 1`` auf einer Verbindung aus dem Pool. Prueft die
            Kette bis in die Datenbank, ohne eine Tabelle anzufassen.
``index``   ``GET /<name>/_health`` des acoustid-index — der Server
            sagt damit, ob der Index existiert und geladen ist.
==========  ==========================================================

**Was bewusst NICHT geprueft wird:** der MusicBrainz-Spiegel. Er darf
fehlen (Invariante §8.7, degradierter Betrieb) — waere er Teil der
Bereitschaft, wuerde ein Ausfall bei MusicBrainz den ganzen Stack als
„nicht bereit" abstempeln und jede Anfrage in ein 503 laufen lassen.

Antwortform (JSON)::

    {"status": "ok", "version": "0.0.1", "checks": {"db": "ok", "index": "ok"}}  # HTTP 200
    {
        "status": "error",
        "version": "0.0.1",
        "checks": {"db": "ok", "index": "Index 'main' laedt noch"},
    }  # HTTP 503
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from acoustid_api import __version__

if TYPE_CHECKING:  # pragma: no cover - nur fuer die Typpruefung
    from acoustid_api.service import ApiService

__all__ = ["HEALTH_PATH", "build_health"]

_LOG = logging.getLogger(__name__)

#: Pfad des Endpunkts. Konservativ gewaehlt: der Unterstrich-Praefix ist im
#: Projekt schon fuer genau diese Rolle in Gebrauch (der acoustid-index
#: bietet ``/_health`` und ``/<index>/_health``), und ausserhalb von
#: ``/v2/`` kann er mit keinem Original-Endpunkt kollidieren.
HEALTH_PATH: Final = "/_health"

#: Kennzeichnung einer bestandenen Pruefung.
_OK: Final = "ok"


def build_health(service: ApiService) -> tuple[dict[str, Any], int]:
    """Prueft Datenbank und Suchindex.

    Args:
        service: Laufzeitumgebung des API-Dienstes.

    Returns:
        Antwortrumpf und HTTP-Status (200 bereit, 503 nicht bereit).
    """
    checks = {"db": _check_db(service), "index": _check_index(service)}
    healthy = all(value == _OK for value in checks.values())
    if not healthy:
        _LOG.warning("Bereitschaftspruefung verneint", extra={"checks": checks})
    return (
        {"status": "ok" if healthy else "error", "version": __version__, "checks": checks},
        200 if healthy else 503,
    )


def _check_db(service: ApiService) -> str:
    """``SELECT 1`` — billiger geht eine echte Abfrage nicht."""
    try:
        with service.pool.connection() as connection:
            connection.execute("SELECT 1")
    except Exception as exc:
        # Jede Ursache heisst hier dasselbe: „nicht bereit". Der Waechter
        # fragt gleich wieder; ein Stacktrace je Sekunde waere Laerm.
        return _reason(exc)
    return _OK


def _check_index(service: ApiService) -> str:
    """``GET /<name>/_health`` — existiert der Index und ist er geladen?

    ``index_health()`` liefert ``False`` fuer „gibt es nicht" (404) und
    „laedt noch" (503); beides heisst hier „nicht bereit". Vor dem ersten
    Bootstrap-Lauf existiert der Index nicht — dann ist der Dienst
    tatsaechlich nicht einsatzbereit, und der Waechter soll das sehen.
    """
    try:
        if not service.index.index_health():
            return f"Index {service.index.index_name!r} fehlt oder laedt noch"
    except Exception as exc:
        return _reason(exc)
    return _OK


def _reason(exc: Exception) -> str:
    """Kurze, einzeilige Begruendung fuer die Antwort und das Log."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:200]
