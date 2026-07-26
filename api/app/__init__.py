"""API-Service von acoustid-offline (Import-Name ``acoustid_api``).

Stand Phase 9: ``GET/POST /v2/lookup`` gegen den eigenen Bestand — ohne
``meta``, ohne Auth und ohne Rate-Limit (beides macht der Waechter). Die
Module:

* :mod:`acoustid_api.main` — HTTP-Schicht (FastAPI): Routen, Rumpf-Grenzen,
  gzip, CORS, Fehlerabbildung.
* :mod:`acoustid_api.params` — Parameter lesen und pruefen.
* :mod:`acoustid_api.formats` — json / jsonp / xml serialisieren.
* :mod:`acoustid_api.errors` — die 19 Original-Fehlercodes.
* :mod:`acoustid_api.lookup` — Antwortaufbau von ``/v2/lookup``.
* :mod:`acoustid_api.matching` — zweistufige Pipeline (Index -> Rescoring).
* :mod:`acoustid_api.store` — Lesezugriffe auf die AcoustID-Postgres.
* :mod:`acoustid_api.service` — Pool, Index-Client, Konfiguration.

Dazugekommen sind ``meta`` samt MusicBrainz-Resolver (Phase 10),
``/v2/submit`` mit :mod:`acoustid_api.submit` (Phase 11) und die
Upstream-Weiterleitung in :mod:`acoustid_api.upstream` (Phase 12).
Es fehlen noch ``/v2/lookup/batch`` und ``/v2/submission_status``
(Phase 13).
"""

__version__ = "0.0.1"
