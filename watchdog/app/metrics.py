"""``GET /metrics`` — Prometheus-Format, nur bei ``metrics.enabled`` (§7).

Was hier steht, ist der Auszug aus der M2.5-Aufgabenliste: *„Lookups,
Cache-Quote, Weckvorgaenge, Prozess-Zustand, Import-Laeufe/-Dauer."*

**Der Endpunkt weckt nie** — dieselbe Zusage wie `/status` (Invariante
§8.2), und sie ist hier genauso baulich erfuellt: gelesen werden
ausschliesslich Zaehler aus dem Speicher und die Zustandsdatenbank auf dem
Cache-Pool. Insbesondere spricht `/metrics` **nicht** mit supervisord: der
Prozess-Zustand kommt aus der Momentaufnahme, die der Poller ohnehin alle
15 Sekunden erhebt (:attr:`~acoustid_watchdog.wake.WakeCoordinator.
process_states`). Ein Scraper im 15-Sekunden-Takt darf keine Last auf dem
Steuerweg erzeugen.

**Per Default aus** (``metrics.enabled: false``, §6). Abgeschaltet
antwortet der Pfad mit **404** und nicht mit 403: der Waechter gibt nach
aussen nicht preis, dass es diesen Endpunkt gibt — dieselbe Haltung wie
bei :data:`~acoustid_watchdog.main.DENIED_PATHS`.

**Kein Fremdpaket.** Das Textformat ist eine Handvoll Zeilen, und
``prometheus_client`` braechte eine eigene Registry samt Prozess-Metriken
mit, die hier niemand will. Die Zaehler leben ohnehin dort, wo sie
entstehen (Cache, Weck-Koordination, Lauf-Historie); dieses Modul liest
sie nur zusammen.

**Namensschema:** Praefix ``musicmeta_``, Einheiten im Namen
(``_seconds``, ``_bytes``), Zaehler auf ``_total``. Das ist die
Prometheus-Konvention und macht die Reihen ohne Dokumentation lesbar.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Final

from acoustid_watchdog import __version__
from acoustid_watchdog.runs import RunKind, latest_run, run_totals
from shared.models import StackState

if TYPE_CHECKING:  # nur fuer die Typannotation
    from acoustid_watchdog.service import WatchdogService

__all__ = ["CONTENT_TYPE", "metric_names", "render"]

#: Content-Type des Prometheus-Textformats (Version 0.0.4).
CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

#: Prozesse, deren Zustand ausgewiesen wird — die Namen kommen aus der
#: Momentaufnahme des Pollers, nicht aus einer eigenen Abfrage.
_UP_STATE: Final = "RUNNING"


def _escape(value: str) -> str:
    """Label-Wert nach Prometheus-Regeln (Backslash, Anfuehrungszeichen, Zeilenumbruch)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: Mapping[str, Any]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{name}="{_escape(str(value))}"' for name, value in pairs.items())
    return "{" + inner + "}"


class _Writer:
    """Sammelt die Zeilen einer Ausgabe — Hilfe, kein Vertrag."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._declared: set[str] = set()

    def metric(
        self,
        name: str,
        value: float | int | None,
        *,
        help_text: str,
        kind: str = "gauge",
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        """Eine Messreihe; ``None`` wird ausgelassen statt als 0 erfunden."""
        if value is None:
            return
        if name not in self._declared:
            self._lines.append(f"# HELP {name} {help_text}")
            self._lines.append(f"# TYPE {name} {kind}")
            self._declared.add(name)
        rendered = f"{value:.6g}" if isinstance(value, float) else str(value)
        self._lines.append(f"{name}{_labels(labels or {})} {rendered}")

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


def render(service: WatchdogService) -> str:
    """Baut die Antwort von ``GET /metrics``.

    Args:
        service: Der laufende Waechter-Dienst. Gelesen werden nur seine
            Zaehler und seine Zustandsdatenbank — kein Netz, kein Socket.
    """
    out = _Writer()
    _build_info(out, service)
    _stack(out, service)
    _lookups(out, service)
    _wakes(out, service)
    _jobs(out, service)
    _notifications(out, service)
    return out.render()


def _build_info(out: _Writer, service: WatchdogService) -> None:
    """Was in diesem Artefakt steckt — dieselben Angaben wie in `/status`."""
    out.metric(
        "musicmeta_build_info",
        1,
        help_text="Version der Instanz und die eingebackenen Fremdkomponenten (v2 §12).",
        labels={
            "version": __version__,
            "postgresql_major": service.settings.pg_major,
            "acoustid_index_commit": service.settings.index_commit or "",
        },
    )


def _stack(out: _Writer, service: WatchdogService) -> None:
    """Stack- und Prozess-Zustand.

    Der Stack-Zustand wird als **eine Reihe je Zustand** ausgegeben (genau
    eine davon steht auf 1). Eine Zahl je Zustandsname waere kompakter,
    aber jede Umnummerierung braeche dann still alle Dashboards.
    """
    current = service.state.state
    for state in StackState:
        out.metric(
            "musicmeta_stack_state",
            1 if state is current else 0,
            help_text="Gefuehrter Zustand des Stacks; genau eine Reihe steht auf 1.",
            labels={"state": state.value},
        )
    for name, statename in service.wake.process_states:
        out.metric(
            "musicmeta_process_up",
            1 if statename == _UP_STATE else 0,
            help_text=(
                "Laeuft dieser Prozess? Momentaufnahme des Zustandsabgleichs (Takt 15 s) — "
                "der Endpunkt fragt supervisord nicht selbst."
            ),
            labels={"program": name},
        )
        out.metric(
            "musicmeta_process_state",
            1,
            help_text="Zustandsname aus supervisord (RUNNING, STOPPED, FATAL, …).",
            labels={"program": name, "state": statename},
        )


def _lookups(out: _Writer, service: WatchdogService) -> None:
    """Lookups und Cache-Quote.

    ``musicmeta_lookups_total`` zaehlt die **durchgelassenen**
    ``/v2/``-Anfragen: Cache-Treffer plus weitergeleitete. Abgewiesene
    (Rate-Limit, ungueltiger Key) sind nicht dabei — sie haben nie eine
    Antwort erzeugt.

    Die Quote selbst wird bewusst **nicht** ausgerechnet: das ist Sache
    der Abfragesprache (``rate(hits) / rate(hits + misses)``), und ein
    vorberechneter Anteil ueber die ganze Prozesslaufzeit waere die
    unbrauchbarere Zahl.
    """
    counters = service.cache.counters
    forwarded = service.activity.requests
    out.metric(
        "musicmeta_lookups_total",
        counters.hits + forwarded,
        help_text="Durchgelassene /v2/-Anfragen (Cache-Treffer plus weitergeleitete).",
        kind="counter",
    )
    out.metric(
        "musicmeta_proxy_requests_total",
        forwarded,
        help_text="An den API-Dienst weitergeleitete Anfragen.",
        kind="counter",
    )
    out.metric(
        "musicmeta_lookup_cache_hits_total",
        counters.hits,
        help_text="Aus dem Lookup-Cache beantwortete Anfragen (ohne Weckvorgang).",
        kind="counter",
    )
    out.metric(
        "musicmeta_lookup_cache_misses_total",
        counters.misses,
        help_text="Cachefaehige Anfragen ohne Treffer.",
        kind="counter",
    )
    out.metric(
        "musicmeta_lookup_cache_stored_total",
        counters.stores,
        help_text="In den Lookup-Cache aufgenommene Antworten.",
        kind="counter",
    )
    out.metric(
        "musicmeta_lookup_cache_evictions_total",
        counters.evictions,
        help_text="Aus Platzgruenden verdraengte Eintraege (LRU, cache.max_size_mb).",
        kind="counter",
    )
    out.metric(
        "musicmeta_lookup_cache_entries",
        service.cache.entries,
        help_text="Eintraege im Lookup-Cache.",
    )
    out.metric(
        "musicmeta_lookup_cache_bytes",
        service.cache.total_bytes,
        help_text="Belegung des Lookup-Caches in Byte.",
    )


def _wakes(out: _Writer, service: WatchdogService) -> None:
    """Weckvorgaenge, Stopps und die Leerlaufuhr."""
    out.metric(
        "musicmeta_wakes_total",
        service.wake.wakes,
        help_text="Begonnene Weckvorgaenge (nicht: wartende Anfragen).",
        kind="counter",
    )
    out.metric(
        "musicmeta_stops_total",
        service.wake.stops,
        help_text="Begonnene Stoppvorgaenge (Idle-Stopp und Scheduler).",
        kind="counter",
    )
    out.metric(
        "musicmeta_idle_seconds",
        service.activity.idle_s,
        help_text="Sekunden seit der letzten /v2/-Anfrage.",
    )
    out.metric(
        "musicmeta_idle_stop_blocked_total",
        service.idle.blocked_by_jobs,
        help_text="Wie oft ein laufender Job den Idle-Stopp aufgeschoben hat (§8.5).",
        kind="counter",
    )


def _jobs(out: _Writer, service: WatchdogService) -> None:
    """Laeufe und Laufdauern je Art (``update_run``)."""
    totals = run_totals(service.db)
    for (kind, outcome), count in sorted(totals.items()):
        out.metric(
            "musicmeta_runs_total",
            count,
            help_text="Laeufe je Art und Ausgang; 'running' zaehlt die noch offenen.",
            kind="counter",
            labels={"kind": kind, "result": outcome},
        )
    for kind in RunKind:
        run = latest_run(service.db, kind)
        if run is None:
            continue
        out.metric(
            "musicmeta_last_run_duration_seconds",
            run.duration_s,
            help_text="Dauer des zuletzt abgeschlossenen Laufs je Art.",
            labels={"kind": kind.value},
        )
    out.metric(
        "musicmeta_job_running",
        1 if service.job_manager.running else 0,
        help_text="Laeuft gerade ein Job? (Genau einer ist moeglich.)",
    )
    out.metric(
        "musicmeta_jobs_triggered_total",
        service.job_manager.triggered,
        help_text="Von diesem Prozess angestossene Laeufe (Scheduler und manuell).",
        kind="counter",
    )


def _notifications(out: _Writer, service: WatchdogService) -> None:
    """Zustellungen und Fehlversuche der Benachrichtigungen."""
    out.metric(
        "musicmeta_notifications_sent_total",
        service.notify.sent,
        help_text="Erfolgreich zugestellte Benachrichtigungen (je Kanal gezaehlt).",
        kind="counter",
    )
    out.metric(
        "musicmeta_notifications_failed_total",
        service.notify.failures,
        help_text="Gescheiterte Zustellversuche.",
        kind="counter",
    )


def metric_names(text: str) -> Iterable[str]:
    """Die Reihennamen einer Ausgabe — fuer Tests und Diagnose."""
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            yield line.split()[2]
