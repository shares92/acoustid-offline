"""Laufzeitumgebung des API-Dienstes: Datenbank-Pool, Index, Konfiguration.

Ein API-Prozess braucht drei langlebige Dinge, und alle drei entstehen genau
einmal beim Start:

* **Postgres-Pool.** Die Lookups sind kurz und rein lesend; ein Pool spart
  den Verbindungsaufbau je Anfrage. Die Verbindungen laufen mit
  ``autocommit`` — ohne Schreibzugriffe gibt es nichts zu committen, und so
  bleibt keine Transaktion offen stehen (waehrend des Delta-Imports wuerde
  das sonst alte Zeilenversionen festhalten).
* **Index-Client.** Ein HTTP-Pool auf den acoustid-index, geteilt von allen
  Anfragen.
* **Laufzeit-Konfiguration.** Aus ihr kommt ``index.query_hashes`` — der
  Wert **muss** derselbe sein, mit dem der Importer indexiert hat; deshalb
  liest die API dieselbe ``config.yaml`` (im Container read-only gemountet)
  und nicht etwa eine eigene Env-Variable.

Der Dienst ist bewusst **synchron**: der Index-Client und psycopg sind es
auch, die Anfragen sind kurz, und das Rescoring ist ohnehin rechen- und
nicht wartegebunden. Die HTTP-Schicht schiebt die Arbeit in den Threadpool
(siehe :mod:`acoustid_api.main`).
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Final, Self

from psycopg_pool import ConnectionPool

from acoustid_api.matching import Matcher
from shared.config import Config, load_config
from shared.env import EnvSettings
from shared.fpindex import FpIndexClient

__all__ = ["DEFAULT_POOL_MAX_SIZE", "DEFAULT_POOL_MIN_SIZE", "ApiService"]

_LOG = logging.getLogger(__name__)

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
    ) -> None:
        self.pool = pool
        self.index = index
        self.config = config
        self.matcher = Matcher(index, query_hashes=config.index.query_hashes)

    @classmethod
    def from_env(
        cls,
        env: EnvSettings | None = None,
        *,
        min_size: int = DEFAULT_POOL_MIN_SIZE,
        max_size: int = DEFAULT_POOL_MAX_SIZE,
    ) -> Self:
        """Baut den Dienst aus den ``AOFF_``-Variablen und der config.yaml.

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
                "query_hashes": config.index.query_hashes,
            },
        )
        pool = ConnectionPool(
            settings.db_dsn().get_secret_value(),
            min_size=min_size,
            max_size=max_size,
            kwargs={"autocommit": True},
            open=False,
        )
        return cls(pool, FpIndexClient.from_env(settings), config)

    def open(self, *, timeout: float = 30.0) -> None:
        """Oeffnet den Datenbank-Pool und wartet auf die erste Verbindung."""
        self.pool.open(wait=True, timeout=timeout)

    def close(self) -> None:
        """Gibt Pool und HTTP-Verbindungen frei."""
        self.pool.close()
        self.index.close()

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
