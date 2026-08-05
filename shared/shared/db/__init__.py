"""Datenbankschicht des gemeinsamen Pakets (AcoustID-Postgres).

Ab Phase 4 enthaelt sie den Migrations-Runner samt Schema-Dateien:

* :func:`shared.db.apply` — ausstehende Migrationen auf einer offenen
  Verbindung anwenden (idempotent, je Migration eine Transaktion).
* :func:`shared.db.apply_from_env` — dasselbe mit Verbindung aus den
  `MMO_DB_*`-Variablen; der uebliche Aufruf beim Start eines Containers.
* Gruppen :data:`shared.db.CORE` (Tabellen) und :data:`shared.db.INDEXES`
  (Sekundaerindizes) — beim Bootstrap wird `indexes` erst nach dem
  Massenimport angewendet (ARCHITECTURE §5.2).

Kommandozeile: ``python -m shared.db --groups core``.
"""

from shared.db.migrations import (
    BOOKKEEPING_TABLE,
    CORE,
    GROUPS,
    INDEXES,
    Migration,
    MigrationDriftError,
    MigrationError,
    MigrationReport,
    applied_versions,
    apply,
    apply_from_env,
    migrations,
    normalise_groups,
    pending,
)

__all__ = [
    "BOOKKEEPING_TABLE",
    "CORE",
    "GROUPS",
    "INDEXES",
    "Migration",
    "MigrationDriftError",
    "MigrationError",
    "MigrationReport",
    "applied_versions",
    "apply",
    "apply_from_env",
    "migrations",
    "normalise_groups",
    "pending",
]
