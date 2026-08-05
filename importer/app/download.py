"""Downloader der Tagesdeltas (ARCHITECTURE §5.1).

Holt die Dateien von ``data.acoustid.org`` in ein lokales Arbeitsverzeichnis
(``MMO_DUMP_DIR``) — robust genug fuer den Bootstrap, der 38.178 Dateien
mit zusammen 414 GB ziehen muss:

* **Atomar:** geladen wird nach ``<name>.part``, erst die vollstaendige und
  gepruefte Datei wird auf den Zielnamen umbenannt. Ein Abbruch hinterlaesst
  nie eine halbe Datei unter dem richtigen Namen.
* **Resume per HTTP-Range:** liegt ein ``.part`` vor, wird ab dessen Groesse
  weitergeladen (``Range: bytes=N-``, Antwort 206). Ignoriert der Server den
  Range-Header (Antwort 200), beginnt die Datei sauber von vorn.
* **Retries mit exponentiellem Backoff:** 5 Versuche, 1 s bis 10 s Pause.
  Abbrechende Verbindungen sind bei dieser Quelle dokumentiert haeufig; jeder
  neue Versuch setzt am ``.part`` an, laedt also nicht von vorn.
* **Groessenvalidierung gegen ``index.json``:** das Verzeichnis-Listing des
  Monats nennt die exakten Bytegroessen und wird einmal je Monat geholt und
  gecacht. Es ist zugleich die Antwort auf „gibt es diesen Tag ueberhaupt?" —
  fehlt der Name dort, ist das die Luecke aus Import-Regel 5
  (:class:`~acoustid_importer.errors.DeltaNotFoundError`).
* **gzip-Integritaetspruefung** nach dem Laden (abschaltbar, siehe
  :meth:`DeltaDownloader.fetch`).
* **Vorhandene Dateien werden uebersprungen**, wenn ihre Groesse zur
  ``index.json`` passt.

Beispiel::

    with DeltaDownloader.from_env() as downloader:
        for result in downloader.fetch_all(plan.files):
            print(result.path, result.size, result.reused)

Der Downloader spricht nur HTTP und Dateisystem — er kennt weder Datenbank
noch Parser.
"""

from __future__ import annotations

import gzip
import logging
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

import httpx

from acoustid_importer.errors import DeltaNotFoundError, DownloadError, SizeMismatchError
from acoustid_importer.streams import BASE_URL, EMPTY_GZ_SIZE, DeltaFile, month_index_url
from shared.env import EnvSettings

__all__ = [
    "CHUNK_BYTES",
    "DEFAULT_BACKOFF_INITIAL_S",
    "DEFAULT_BACKOFF_MAX_S",
    "DEFAULT_MAX_ATTEMPTS",
    "USER_AGENT",
    "DeltaDownloader",
    "Download",
]

_LOG = logging.getLogger(__name__)

#: Versuche je Datei (der erste plus vier Wiederholungen).
DEFAULT_MAX_ATTEMPTS: Final = 5

#: Erste Wartezeit; verdoppelt sich je Versuch bis :data:`DEFAULT_BACKOFF_MAX_S`.
DEFAULT_BACKOFF_INITIAL_S: Final = 1.0

#: Obergrenze der Wartezeit zwischen zwei Versuchen.
DEFAULT_BACKOFF_MAX_S: Final = 10.0

#: Blockgroesse der gzip-Pruefung (1 MiB). Der Download selbst schreibt jeden
#: Netzblock sofort, damit ein Abbruch keinen Fortschritt verwirft.
CHUNK_BYTES: Final = 1 << 20

#: Kennung gegenueber der Quelle — hoeflich und zuordenbar (§12, Punkt 9:
#: Fair-Use gegenueber AcoustID OUe).
USER_AGENT: Final = "acoustid-offline-importer/1.0 (+https://github.com/shares92/acoustid-offline)"

#: Leseschranke; grosszuegig, weil einzelne Dateien mehrere GB haben.
_DEFAULT_READ_TIMEOUT_S: Final = 60.0
_DEFAULT_CONNECT_TIMEOUT_S: Final = 10.0

#: Statuscodes, bei denen ein weiterer Versuch sinnvoll ist.
_RETRY_STATUS: Final = frozenset(
    {
        HTTPStatus.REQUEST_TIMEOUT,
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }
)


@dataclass(frozen=True, slots=True)
class Download:
    """Ergebnis eines :meth:`DeltaDownloader.fetch`."""

    file: DeltaFile
    path: Path
    size: int
    #: Datei lag bereits vollstaendig vor und wurde nicht erneut geladen.
    reused: bool = False
    #: Ein vorhandenes ``.part`` wurde per Range fortgesetzt.
    resumed: bool = False
    #: Zahl der benoetigten Versuche (1 = auf Anhieb).
    attempts: int = 1

    @property
    def empty(self) -> bool:
        """Leere Tagesdatei (23 Byte) — regulaer, keine Luecke."""
        return self.size == EMPTY_GZ_SIZE


class _RetryError(Exception):
    """Intern: dieser Versuch ist gescheitert, ein weiterer lohnt sich."""


class DeltaDownloader:
    """Laedt Tagesdateien nach ``dest_dir`` — resumierbar und geprueft."""

    def __init__(
        self,
        dest_dir: Path,
        *,
        base_url: str = BASE_URL,
        client: httpx.Client | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_initial_s: float = DEFAULT_BACKOFF_INITIAL_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
        read_timeout_s: float = _DEFAULT_READ_TIMEOUT_S,
        connect_timeout_s: float = _DEFAULT_CONNECT_TIMEOUT_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        Args:
            dest_dir: Arbeitsverzeichnis (``MMO_DUMP_DIR``); wird bei Bedarf
                angelegt.
            base_url: Wurzel der Quelle; in Tests ein lokaler Server.
            client: Vorhandener ``httpx.Client`` (Tests, gemeinsamer Pool).
                Wird dann **nicht** von :meth:`close` geschlossen.
            max_attempts: Versuche je Datei.
            backoff_initial_s: Erste Wartezeit zwischen zwei Versuchen.
            backoff_max_s: Obergrenze der Wartezeit.
            read_timeout_s: Leseschranke einer einzelnen Antwort.
            connect_timeout_s: Schranke fuer den Verbindungsaufbau.
            sleep: Wartefunktion; in Tests ersetzbar.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts muss mindestens 1 sein")
        self.dest_dir = dest_dir
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self._sleep = sleep
        self._timeout = httpx.Timeout(read_timeout_s, connect=connect_timeout_s)
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=self._timeout, follow_redirects=True)
        self._index_cache: dict[str, Mapping[str, int]] = {}

    @classmethod
    def from_env(cls, env: EnvSettings | None = None, **kwargs: Any) -> Self:
        """Baut den Downloader mit ``MMO_DUMP_DIR`` als Zielverzeichnis."""
        settings = env or EnvSettings.from_env()
        return cls(settings.dump_dir, **kwargs)

    # --- Lebenszyklus ------------------------------------------------------

    def close(self) -> None:
        """Schliesst den HTTP-Pool, falls dieser Downloader ihn angelegt hat."""
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
        return f"{type(self).__name__}(dest_dir={str(self.dest_dir)!r}, base_url={self.base_url!r})"

    # --- Verzeichnis-Listing ----------------------------------------------

    def month_index(self, day: date) -> Mapping[str, int]:
        """Dateiname -> exakte Bytegroesse fuer den Monat von ``day``.

        Wird je Monat genau einmal geholt und gecacht. Verzeichniseintraege
        (``{"name": "2026-07/"}`` ohne ``size``) werden uebergangen.

        Raises:
            DeltaNotFoundError: Es gibt kein ``index.json`` fuer den Monat.
            DownloadError: Das Listing ist nicht erreichbar oder unlesbar.
        """
        key = f"{day.year:04d}-{day.month:02d}"
        cached = self._index_cache.get(key)
        if cached is not None:
            return cached

        url = month_index_url(day, self.base_url)
        payload = self._get_json(url)
        if not isinstance(payload, list):
            raise DownloadError(f"{url}: Liste erwartet, {type(payload).__name__} bekommen")
        sizes: dict[str, int] = {}
        for entry in payload:
            if not isinstance(entry, dict):
                raise DownloadError(f"{url}: unerwarteter Eintrag {entry!r}")
            name, size = entry.get("name"), entry.get("size")
            if isinstance(name, str) and isinstance(size, int):
                sizes[name] = size
        self._index_cache[key] = sizes
        _LOG.debug("Monats-Listing geladen", extra={"month": key, "entries": len(sizes)})
        return sizes

    def clear_index_cache(self) -> None:
        """Vergisst alle gecachten Monats-Listings (der laufende Monat waechst)."""
        self._index_cache.clear()

    def expected_size(self, file: DeltaFile) -> int:
        """Bytegroesse laut ``index.json``.

        Raises:
            DeltaNotFoundError: Die Datei steht nicht im Listing — fuer einen
                Tag in der Vergangenheit ist das eine Luecke (Regel 5).
        """
        sizes = self.month_index(file.day)
        size = sizes.get(file.name)
        if size is None:
            raise DeltaNotFoundError(
                f"{file.name} steht nicht im index.json von {file.month} — "
                "die Datei existiert upstream nicht"
            )
        return size

    # --- Laden -------------------------------------------------------------

    def path_for(self, file: DeltaFile) -> Path:
        """Zielpfad im Arbeitsverzeichnis."""
        return self.dest_dir / file.name

    def fetch(self, file: DeltaFile, *, verify_gzip: bool = True) -> Download:
        """Holt eine Tagesdatei (oder stellt fest, dass sie schon da ist).

        Args:
            file: Tag und Strom.
            verify_gzip: Nach dem Laden den gzip-Strom einmal vollstaendig
                dekomprimieren. Fuer den Bootstrap abschaltbar — dort liest
                der Parser jede Datei ohnehin direkt danach und meldet einen
                kaputten Strom als Parse-Fehler.

        Returns:
            :class:`Download` mit Pfad, Groesse und wie es dazu kam.

        Raises:
            DeltaNotFoundError: Die Datei existiert upstream nicht.
            SizeMismatchError: Die geladene Datei hat die falsche Groesse.
            DownloadError: Nach allen Versuchen kein vollstaendiger Download.
        """
        expected = self.expected_size(file)
        target = self.path_for(file)
        if target.exists():
            actual = target.stat().st_size
            if actual == expected:
                # Ein liegengebliebener Rest zur fertigen Datei ist Muell und
                # kostet beim Bootstrap echten Platz.
                target.with_name(target.name + ".part").unlink(missing_ok=True)
                _LOG.debug(
                    "Tagesdatei liegt bereits vor",
                    extra={"delta_file": file.name, "bytes": actual},
                )
                return Download(file, target, actual, reused=True, attempts=0)
            _LOG.warning(
                "Vorhandene Tagesdatei hat die falsche Groesse — wird neu geladen",
                extra={"delta_file": file.name, "bytes": actual, "expected_bytes": expected},
            )
            target.unlink()

        self.dest_dir.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        url = file.url(self.base_url)
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                resumed = self._download_once(url, part, expected)
            except _RetryError as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    break
                delay = min(
                    self.backoff_initial_s * 2 ** (attempt - 1),
                    self.backoff_max_s,
                )
                _LOG.warning(
                    "Download abgebrochen — neuer Versuch",
                    extra={
                        "delta_file": file.name,
                        "attempt": attempt,
                        "max_attempts": self.max_attempts,
                        "delay_s": delay,
                        "reason": str(exc),
                    },
                )
                self._sleep(delay)
                continue

            if verify_gzip:
                self._verify_gzip(part, file)
            part.replace(target)
            _LOG.info(
                "Tagesdatei geladen",
                extra={
                    "delta_file": file.name,
                    "bytes": expected,
                    "attempt": attempt,
                    "resumed": resumed,
                },
            )
            return Download(file, target, expected, resumed=resumed, attempts=attempt)

        raise DownloadError(
            f"{file.name}: nach {self.max_attempts} Versuchen nicht vollstaendig geladen "
            f"({last_error})"
        ) from last_error

    def fetch_all(
        self, files: Iterable[DeltaFile], *, verify_gzip: bool = True
    ) -> Iterator[Download]:
        """Holt mehrere Dateien der Reihe nach (Reihenfolge bleibt erhalten)."""
        for file in files:
            yield self.fetch(file, verify_gzip=verify_gzip)

    # --- Innenleben --------------------------------------------------------

    def _download_once(self, url: str, part: Path, expected: int) -> bool:
        """Ein Versuch. Gibt zurueck, ob per Range fortgesetzt wurde.

        Raises:
            _RetryError: Netzfehler, Fehlerstatus mit Aussicht auf Besserung oder
                abgebrochene Uebertragung.
            DeltaNotFoundError, SizeMismatchError, DownloadError: endgueltig.
        """
        offset = part.stat().st_size if part.exists() else 0
        if offset >= expected:
            # Unbrauchbarer Rest (Datei geaendert, Abbruch nach dem Rename):
            # sauber neu anfangen statt an einem falschen Stand kleben.
            part.unlink()
            offset = 0

        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"

        try:
            with self._client.stream("GET", url, headers=headers) as response:
                resumed = self._check_response(response, url, offset, expected)
                mode = "ab" if resumed else "wb"
                with part.open(mode) as handle:
                    # `iter_raw()` ohne Blockgroesse: jeder Netzblock wird
                    # sofort geschrieben. `iter_bytes(1 MiB)` wuerde bis zur
                    # vollen Blockgroesse puffern und den Puffer beim Abbruch
                    # verwerfen — genau der Fortschritt, den das Resume braucht.
                    for chunk in response.iter_raw():
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            raise _RetryError(f"Uebertragung gescheitert: {exc}") from exc

        written = part.stat().st_size
        if written < expected:
            raise _RetryError(f"unvollstaendig: {written} von {expected} Byte")
        if written > expected:
            part.unlink(missing_ok=True)
            raise SizeMismatchError(
                f"{url}: {written} Byte geladen, laut index.json sind es {expected} — "
                "die Quelldatei passt nicht zum Listing"
            )
        return resumed

    def _check_response(
        self, response: httpx.Response, url: str, offset: int, expected: int
    ) -> bool:
        """Prueft den Antwortkopf. Gibt zurueck, ob fortgesetzt wird."""
        status = response.status_code
        if status == HTTPStatus.NOT_FOUND:
            raise DeltaNotFoundError(f"{url}: HTTP 404 — die Datei existiert dort nicht")
        if status in _RETRY_STATUS:
            raise _RetryError(f"HTTP {status}")
        if status >= HTTPStatus.BAD_REQUEST:
            raise DownloadError(f"{url}: HTTP {status}")

        if offset and status == HTTPStatus.PARTIAL_CONTENT:
            total = _range_total(response.headers.get("content-range"))
            if total is not None and total != expected:
                raise SizeMismatchError(
                    f"{url}: Server meldet {total} Byte, laut index.json sind es {expected}"
                )
            return True

        # 200 trotz Range-Wunsch: der Server kann (oder will) nicht
        # fortsetzen -> die Datei kommt komplett, das .part wird ueberschrieben.
        if offset:
            _LOG.info(
                "Server ignoriert den Range-Wunsch — Datei wird neu geladen",
                extra={"url": url, "status": status, "offset": offset},
            )
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) != expected:
            raise SizeMismatchError(
                f"{url}: Server meldet {declared} Byte, laut index.json sind es {expected}"
            )
        return False

    def _get_json(self, url: str) -> Any:
        """GET mit denselben Retries wie ein Download, Antwort als JSON."""
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._client.get(
                    url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
                )
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code == HTTPStatus.NOT_FOUND:
                    raise DeltaNotFoundError(f"{url}: HTTP 404 — kein Listing fuer diesen Monat")
                if response.status_code not in _RETRY_STATUS:
                    if response.status_code >= HTTPStatus.BAD_REQUEST:
                        raise DownloadError(f"{url}: HTTP {response.status_code}")
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise DownloadError(f"{url}: Antwort ist kein JSON ({exc})") from exc
                last_error = DownloadError(f"{url}: HTTP {response.status_code}")

            if attempt == self.max_attempts:
                break
            delay = min(self.backoff_initial_s * 2 ** (attempt - 1), self.backoff_max_s)
            _LOG.warning(
                "Listing nicht erreichbar — neuer Versuch",
                extra={"url": url, "attempt": attempt, "delay_s": delay},
            )
            self._sleep(delay)
        raise DownloadError(
            f"{url}: nach {self.max_attempts} Versuchen kein Listing ({last_error})"
        )

    @staticmethod
    def _verify_gzip(path: Path, file: DeltaFile) -> None:
        """Dekomprimiert den Strom einmal vollstaendig.

        Raises:
            DownloadError: Der gzip-Strom ist defekt; die Datei wird
                verworfen, damit der naechste Lauf sie frisch holt.
        """
        try:
            with gzip.open(path, "rb") as handle:
                while handle.read(CHUNK_BYTES):
                    pass
        except (OSError, EOFError) as exc:
            path.unlink(missing_ok=True)
            raise DownloadError(f"{file.name}: gzip-Strom defekt ({exc})") from exc


def _range_total(content_range: str | None) -> int | None:
    """Gesamtgroesse aus einem ``Content-Range: bytes 100-199/423795``."""
    if not content_range or "/" not in content_range:
        return None
    total = content_range.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None
