"""Gemeinsames Fundament von acoustid-offline.

Inhalt ab Phase 3:

* `shared.config` — Schema, Laden und atomares Schreiben der `config.yaml`
  (ARCHITECTURE §6).
* `shared.env` — Bootstrap ueber `MMO_`-Umgebungsvariablen (Pfade, Ports,
  DB-/Index-Zugaenge), bewusst getrennt von der config.yaml.
* `shared.logging_setup` — strukturiertes JSON-Logging nach stderr.
* `shared.models` — gemeinsame Enums (Auth-/Submit-Modus, Submission-Status,
  Stack-Zustaende).

Ab Phase 4 zusaetzlich:

* `shared.db` — Schema-Migrationen der AcoustID-Postgres (ARCHITECTURE §5.2):
  `shared.db.apply` bzw. `shared.db.apply_from_env`, Gruppen `core` und
  `indexes`. Bewusst NICHT hier re-exportiert, damit der Waechter (der keine
  Postgres braucht) den Treiber nicht mitladen muss.

Die haeufig gebrauchten Namen sind hier re-exportiert, sodass
`from shared import Config, load_config, setup_logging` genuegt.
"""

from shared.config import (
    Config,
    ConfigError,
    ConfigFileError,
    ConfigValidationError,
    config_to_dict,
    load_config,
    save_config,
)
from shared.env import ENV_PREFIX, EnvError, EnvSettings, env_var_name
from shared.logging_setup import JsonLogFormatter, setup_logging
from shared.models import AuthMode, StackState, SubmissionStatus, SubmitMode

__version__ = "0.0.1"

__all__ = [
    "ENV_PREFIX",
    "AuthMode",
    "Config",
    "ConfigError",
    "ConfigFileError",
    "ConfigValidationError",
    "EnvError",
    "EnvSettings",
    "JsonLogFormatter",
    "StackState",
    "SubmissionStatus",
    "SubmitMode",
    "__version__",
    "config_to_dict",
    "env_var_name",
    "load_config",
    "save_config",
    "setup_logging",
]
