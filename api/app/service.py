"""Laufzeitumgebung des API-Dienstes: Datenbank-Pool, Index, MusicBrainz.

Ein API-Prozess braucht fuenf langlebige Dinge, und alle entstehen genau
einmal beim Start:

* **Postgres-Pool.** Die Lookups sind kurz und rein lesend; ein Pool spart
  den Verbindungsaufbau je Anfrage. Die Verbindungen laufen mit
  ``autocommit`` — ohne Schreibzugriffe gibt es nichts zu committen, und so
  bleibt keine Transaktion offen stehen (waehrend des Delta-Imports wuerde
  das sonst alte Zeilenversionen festhalten).
* **Index-Client.** Ein HTTP-Pool auf den acoustid-index, geteilt von allen
  Anfragen.
* **MusicBrainz-Client.** Ein zweiter, kleiner Postgres-Pool auf den
  Spiegel des Betreibers (``mb.dsn``). Er ist **optional**: ohne DSN
  bleibt er ``None``, und der Lookup antwortet dauerhaft ohne Metadaten.
  Beim Start laeuft einmal der Schema-Selfcheck — er wirft nie, ein
  unerreichbarer oder abweichender Spiegel darf den Dienst nicht am
  Starten hindern (Invariante §8.7).
* **Upstream-Weiterleiter.** Nur im Modus ``local+upstream`` (sonst
  ``None``): ein HTTP-Pool auf api.acoustid.org **mit gemeinsamer Drossel**.
  Er gehoert genau deshalb hierher und nicht in die Anfrage: die Grenze von
  drei Anfragen je Sekunde gilt fuer den ganzen Prozess, nicht je Thread
  (:mod:`acoustid_api.upstream`).
* **Laufzeit-Konfiguration.** Aus ihr kommt ``acoustid.index.query_hashes`` — der
  Wert **muss** derselbe sein, mit dem der Importer indexiert hat; deshalb
  liest die API dieselbe ``config.yaml`` (im Container read-only gemountet)
  und nicht etwa eine eigene Env-Variable. Dazu ``mb.dsn`` und
  ``mb.keep_submitted_mbid``.

Der Dienst ist bewusst **synchron**: der Index-Client und psycopg sind es
auch, die Anfragen sind kurz, und das Rescoring ist ohnehin rechen- und
nicht wartegebunden. Die HTTP-Schicht schiebt die Arbeit in den Threadpool
(siehe :mod:`acoustid_api.main`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from psycopg_pool import ConnectionPool

from acoustid_api.matching import Matcher
from acoustid_api.upstream import UpstreamForwarder
from shared.config import Config, load_config
from shared.env import EnvSettings
from shared.fpindex import FpIndexClient
from shared.mb import MbClient

__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_POOL_MAX_SIZE",
    "DEFAULT_POOL_MIN_SIZE",
    "ApiService",
]

_LOG = logging.getLogger(__name__)

#: Datenverzeichnis, wenn keines mitgegeben wird — derselbe Vorgabewert wie
#: in :mod:`shared.env` (``MMO_DATA_DIR``). Er steht hier noch einmal, damit
#: ein von Hand gebauter Dienst (Tests) nicht die ganze Umgebung braucht.
DEFAULT_DATA_DIR: Final = Path("/config")

#: Verbindungen, die immer bereitstehen.
DEFAULT_POOL_MIN_SIZE: Final = 1

#: Obergrenze des Pools. Eine Privatinstanz sieht selten mehr als eine
#: Handvoll gleichzeitiger Lookups; mehr Verbindungen wuerden nur die
#: Postgres belasten.
DEFAULT_POOL_MAX_SIZE: Final = 8


class ApiService:
    """Haelt die langlebigen Ressourcen eines API-Prozesses."""

    def __init__(
        self,
        pool: ConnectionPool,
        index: FpIndexClient,
        config: Config,
        mb: MbClient | None = None,
        upstream: UpstreamForwarder | None = None,
        *,
        data_dir: Path = DEFAULT_DATA_DIR,
    ) -> None:
        self.pool = pool
        self.index = index
        self.config = config
        #: Der ``/config``-Mount (``MMO_DATA_DIR``). Der Dienst schreibt
        #: dort nichts — er **liest** eine Marke: waehrend eines
        #: Delta-Imports wird die Indexierung eigener Einreichungen
        #: zurueckgestellt (M2.5, :data:`acoustid_api.submit.
        #: INDEX_BUSY_FILENAME`).
        self.data_dir = data_dir
        #: MusicBrainz-Spiegel; ``None`` heisst „nicht konfiguriert".
        self.mb = mb
        #: Upstream-Weiterleitung; ``None`` ausserhalb von ``local+upstream``.
        #: Ein Prozess, ein Weiterleiter — die Drossel gilt prozessweit.
        self.upstream = upstream or UpstreamForwarder.from_config(config)
        self.matcher = Matcher(index, query_hashes=config.acoustid.index.query_hashes)

    @classmethod
    def from_env(
        cls,
        env: EnvSettings | None = None,
        *,
        min_size: int = DEFAULT_POOL_MIN_SIZE,
        max_size: int = DEFAULT_POOL_MAX_SIZE,
    ) -> Self:
        """Baut den Dienst aus den ``MMO_``-Variablen und der config.yaml.

        Der Pool wird **noch nicht** geoeffnet — das macht :meth:`open`, damit
        ein Start ohne laufende Datenbank nicht schon beim Import scheitert.
        """
        settings = env or EnvSettings.from_env()
        config = load_config(settings.config_path)
        _LOG.info(
            "API-Dienst konfiguriert",
            extra={
                "index_url": settings.index_url,
                "index_name": settings.index_name,
                "query_hashes": config.acoustid.index.query_hashes,
                "mb_configured": config.mb.configured,
                # Der Modus, nie der Schluessel (ARCHITECTURE §6).
                "submit_mode": config.acoustid.submit.mode.value,
            },
        )
        pool = ConnectionPool(
            settings.db_dsn().get_secret_value(),
            min_size=min_size,
            max_size=max_size,
            kwargs={"autocommit": True},
            open=False,
        )
        return cls(
            pool,
            FpIndexClient.from_env(settings),
            config,
            MbClient.from_config(config),
            data_dir=settings.data_dir,
        )

    def open(self, *, timeout: float = 30.0) -> None:
        """Oeffnet die Pools und prueft den MusicBrainz-Spiegel.

        Auf die **eigene** Datenbank wird gewartet — ohne sie kann der
        Dienst nichts. Auf den MusicBrainz-Spiegel nicht: sein Pool oeffnet
        ohne Wartezeit, und der Selfcheck meldet einen Ausfall nur ins Log
        (Invariante §8.7).
        """
        self.pool.open(wait=True, timeout=timeout)
        if self.mb is not None:
            self.mb.open()
            self.mb.startup_check()

    def close(self) -> None:
        """Gibt Pools und HTTP-Verbindungen frei."""
        self.pool.close()
        self.index.close()
        if self.mb is not None:
            self.mb.close()
        if self.upstream is not None:
            self.upstream.close()

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
