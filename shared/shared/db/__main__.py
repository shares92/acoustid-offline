"""Migrationen von der Kommandozeile anwenden.

    python -m shared.db                     # alle Gruppen (Normalfall)
    python -m shared.db --groups core       # Bootstrap: vor dem Massenimport
    python -m shared.db --groups indexes    # Bootstrap: nach dem Massenimport
    python -m shared.db --list              # nur zeigen, nichts anwenden

Zugangsdaten kommen aus den `AOFF_DB_*`-Variablen (ARCHITECTURE §6).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from shared.db.migrations import (
    GROUPS,
    MigrationError,
    apply_from_env,
    migrations,
    normalise_groups,
)
from shared.env import EnvError, EnvSettings
from shared.logging_setup import setup_logging

SERVICE_NAME = "acoustid-migrate"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m shared.db",
        description="Schema-Migrationen der AcoustID-Postgres anwenden.",
    )
    parser.add_argument(
        "--groups",
        default=",".join(GROUPS),
        help=(
            "Kommaliste der Gruppen in beliebiger Reihenfolge "
            f"(bekannt: {', '.join(GROUPS)}; Default: alle)"
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="nur die Migrationen der Gruppen auflisten, nichts anwenden",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Einstiegspunkt; liefert den Exit-Code."""
    args = _parse_args(argv)
    groups = [part.strip() for part in args.groups.split(",") if part.strip()]

    try:
        settings = EnvSettings.from_env()
    except EnvError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 2
    setup_logging(SERVICE_NAME, settings.log_level)

    try:
        if args.list:
            for migration in migrations(groups):
                print(f"{migration.group:8} {migration.version}")
            return 0
        report = apply_from_env(groups=normalise_groups(groups), settings=settings)
    except (EnvError, MigrationError) as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 2

    print(
        f"Gruppen {', '.join(report.groups)}: "
        f"{len(report.applied)} angewendet, {len(report.skipped)} bereits vorhanden"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
