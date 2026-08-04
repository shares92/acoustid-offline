"""Docker-Engine-API ueber den gemounteten Unix-Socket (DECISIONS 2026-07-25).

Der **einzige** Ort im Projekt, an dem Docker gesteuert wird (Invariante
§8.1: „Der Waechter weckt, sonst niemand"). Das Modul kennt weder
Container-Namen des Projekts noch die Weck-Logik — es kann genau drei
Dinge, und zwar fuer beliebige Container:

======================================  ===================================
``GET  /containers/<name>/json``        Zustand eines Containers
``POST /containers/<name>/start``       starten (204; 304 = lief schon)
``POST /containers/<name>/stop``        stoppen (204; 304 = stand schon)
======================================  ===================================

**Warum keine Fremdbibliothek.** Der Entscheid 2026-07-25 nennt als
Mitigation des docker.sock-Risikos ausdruecklich „minimalen Code im
Waechter". Die offizielle Bibliothek (`docker`) bringt einen vollstaendigen
Client fuer Images, Netze, Volumes, Exec und Swarm mit — in genau dem
Container, der den Socket haelt, also die groesste Angriffsflaeche des
Projekts. Gebraucht werden drei Routen ohne jede Sonderbehandlung:
die Engine-API ist eine gewoehnliche HTTP-API, und ``httpx`` (ueber
``shared`` ohnehin im Baum) spricht Unix-Sockets von Haus aus
(``httpx.HTTPTransport(uds=...)``). Das Ergebnis ist ein Modul, das man in
einer Sitzung ganz liest — die Voraussetzung dafuer, dass die
Risiko-Abwaegung der Architektur-Session traegt.

**Ohne Versionspraefix.** Die Engine akzeptiert Pfade sowohl mit
(``/v1.51/containers/...``) als auch ohne Version und behandelt letztere
als „aktuelle Version des Daemons". Ein fest verdrahtetes Praefix wuerde
gegen zu alte **und** zu neue Daemons brechen; ein Aushandeln waere ein
vierter Aufruf ohne Gegenwert.

**Synchron**, wie der Index-Client in ``shared``: die Aufrufe sind kurz,
und die HTTP-Schicht des Waechters schiebt sie ohnehin in den Threadpool.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from http import HTTPStatus
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import quote

import httpx

from acoustid_watchdog.control import ProcessControlError

__all__ = [
    "DEFAULT_STOP_TIMEOUT_S",
    "DEFAULT_TIMEOUT_S",
    "DOCKER_SOCKET",
    "ContainerNotFoundError",
    "ContainerState",
    "DockerClient",
    "DockerError",
    "DockerUnavailableError",
]

_LOG = logging.getLogger(__name__)

#: Pfad des Docker-Sockets im Waechter-Container. Fest verdrahtet wie die
#: Container-Namen (ARCHITECTURE §6 „Feste Werte"): der Mount steht in
#: ``docker-compose.watchdog.yml``, eine eigene ``AOFF_``-Variable haette
#: keinen zweiten moeglichen Wert (und `.env.example`/``shared.env`` sind
#: testgekoppelt — Muster aus DECISIONS 2026-08-01, Punkt 7).
DOCKER_SOCKET: Final = "/var/run/docker.sock"

#: Die Adresse ist der Socket; der Hostname steht nur in der ``Host``-Zeile.
_BASE_URL: Final = "http://docker"

#: Leseschranke fuer Inspect und Start. Ein gesunder Daemon antwortet in
#: Millisekunden; laenger wartet niemand sinnvoll auf eine lokale Antwort.
DEFAULT_TIMEOUT_S: Final = 30.0

#: Frist, die der Daemon dem Container nach ``SIGTERM`` laesst, bevor er
#: ``SIGKILL`` schickt (Parameter ``t`` der Engine-API). Grosszuegiger als
#: Dockers 10-s-Default: die Postgres soll ihren Checkpoint schreiben
#: duerfen. Fuer den Importer-Job gilt ab Phase 19 ein eigener, noch
#: groesserer Wert (Hinweis aus Phase 8).
DEFAULT_STOP_TIMEOUT_S: Final = 60


class DockerError(ProcessControlError):
    """Der Daemon konnte einen Auftrag nicht ausfuehren.

    Unterklasse der technikfreien Basis
    :class:`~acoustid_watchdog.control.ProcessControlError`: die Weck-Logik
    faengt nur die Basis, dieses Modul benennt den Grund genauer.
    """


class DockerUnavailableError(DockerError):
    """Der Socket antwortet nicht — Mount fehlt, Daemon steht.

    Bewusst eine eigene Klasse: das ist ein Betriebsfehler des Wirts, kein
    Fehler des angefragten Containers.
    """


class ContainerNotFoundError(DockerError):
    """Der Container existiert nicht (HTTP 404).

    Im Betrieb heisst das: der Stack wurde nie mit ``docker compose up``
    angelegt. Der Waechter startet Container, er erzeugt sie nicht.
    """


@dataclass(frozen=True, slots=True)
class ContainerState:
    """Was ``GET /containers/<name>/json`` ueber den Zustand sagt."""

    name: str
    #: ``created``, ``running``, ``restarting``, ``exited``, ``paused``, …
    status: str
    running: bool
    #: ``healthy``/``unhealthy``/``starting`` — ``None``, wenn das Image
    #: keinen Healthcheck hat (die Bereitschaft prueft ohnehin der
    #: Waechter selbst, :mod:`acoustid_watchdog.wake`).
    health: str | None = None

    @classmethod
    def from_payload(cls, name: str, payload: Any) -> Self:
        state = payload.get("State") if isinstance(payload, dict) else None
        if not isinstance(state, dict):
            raise DockerError(f"Antwort zu {name!r} enthaelt kein 'State'-Objekt")
        health = state.get("Health")
        reported = health["Status"] if isinstance(health, dict) and "Status" in health else None
        return cls(
            name=name,
            status=str(state.get("Status", "unknown")),
            running=bool(state.get("Running", False)),
            health=None if reported is None else str(reported),
        )


class DockerClient:
    """Drei Operationen auf der Engine-API, mehr nicht."""

    def __init__(
        self,
        socket_path: str = DOCKER_SOCKET,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        """
        Args:
            socket_path: Unix-Socket des Daemons.
            timeout_s: Leseschranke aller Aufrufe. ``stop`` bekommt die
                Abschaltfrist zusaetzlich aufgeschlagen — sonst liefe unsere
                Frist ab, waehrend der Daemon noch geduldig wartet.
            client: Vorhandener ``httpx.Client`` (Tests). Wird dann **nicht**
                von :meth:`close` geschlossen.
        """
        self.socket_path = socket_path
        self._timeout_s = timeout_s
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=_BASE_URL,
            timeout=timeout_s,
            transport=httpx.HTTPTransport(uds=socket_path),
        )

    # --- Lebenszyklus -------------------------------------------------------

    def close(self) -> None:
        """Schliesst den HTTP-Pool, falls dieser Client ihn angelegt hat."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(socket_path={self.socket_path!r})"

    # --- Operationen --------------------------------------------------------

    def inspect(self, name: str) -> ContainerState:
        """Zustand eines Containers.

        Raises:
            ContainerNotFoundError: Der Container existiert nicht.
            DockerUnavailableError: Der Socket antwortet nicht.
        """
        response = self._request("GET", f"/containers/{quote(name, safe='')}/json")
        try:
            payload = json.loads(response.content)
        except ValueError as exc:
            raise DockerError(f"Antwort zu {name!r} ist kein JSON: {exc}") from exc
        return ContainerState.from_payload(name, payload)

    def start(self, name: str) -> bool:
        """Startet einen Container.

        Returns:
            ``True``, wenn dieser Aufruf ihn gestartet hat; ``False``, wenn er
            schon lief (HTTP 304). Der Aufruf ist damit idempotent — genau
            das braucht ein Weckvorgang, den mehrere Anfragen ausloesen
            koennen.

        Raises:
            ContainerNotFoundError: Der Container existiert nicht.
            DockerUnavailableError: Der Socket antwortet nicht.
        """
        response = self._request("POST", f"/containers/{quote(name, safe='')}/start")
        started = response.status_code != HTTPStatus.NOT_MODIFIED
        _LOG.info(
            "Container gestartet" if started else "Container lief bereits",
            extra={"container": name},
        )
        return started

    def stop(self, name: str, *, timeout_s: int = DEFAULT_STOP_TIMEOUT_S) -> bool:
        """Stoppt einen Container.

        Args:
            timeout_s: Frist zwischen ``SIGTERM`` und ``SIGKILL``.

        Returns:
            ``True``, wenn dieser Aufruf ihn gestoppt hat; ``False``, wenn er
            schon stand (HTTP 304).
        """
        response = self._request(
            "POST",
            f"/containers/{quote(name, safe='')}/stop",
            params={"t": timeout_s},
            # Der Daemon antwortet erst, wenn der Container steht.
            read_timeout_s=self._timeout_s + timeout_s,
        )
        stopped = response.status_code != HTTPStatus.NOT_MODIFIED
        _LOG.info(
            "Container gestoppt" if stopped else "Container stand bereits",
            extra={"container": name},
        )
        return stopped

    # --- Transport ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        read_timeout_s: float | None = None,
    ) -> httpx.Response:
        """Einziger IO-Punkt des Moduls."""
        try:
            response = self._client.request(
                method,
                f"{_BASE_URL}{path}",
                params=params,
                timeout=read_timeout_s if read_timeout_s is not None else self._timeout_s,
            )
        except httpx.HTTPError as exc:
            raise DockerUnavailableError(
                f"{method} {path}: keine Antwort vom Docker-Daemon ueber {self.socket_path} ({exc})"
            ) from exc
        if response.status_code == HTTPStatus.NOT_FOUND:
            raise ContainerNotFoundError(f"{method} {path}: {_message(response)}")
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            raise DockerError(
                f"{method} {path}: HTTP {response.status_code} — {_message(response)}"
            )
        return response


def _message(response: httpx.Response) -> str:
    """Die Fehlermeldung des Daemons (``{"message": "..."}``) oder der Rumpf."""
    try:
        payload = json.loads(response.content)
    except ValueError:
        return response.text.strip() or "(leere Antwort)"
    if isinstance(payload, dict) and "message" in payload:
        return str(payload["message"])
    return response.text.strip()
