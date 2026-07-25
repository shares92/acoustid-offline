# Test-Fixtures: AcoustID-Dump-Dateien

Dieses Verzeichnis enthält (lokal, **nicht im Repo**) neun Original-Tagesdateien
der öffentlichen AcoustID-Delta-Exporte. Sie sind die Referenz für Parser-,
Import- und Schema-Tests: an ihnen wurde in Phase 0 das Delta-Format verifiziert
(ARCHITECTURE.md §5.1).

## Warum die Dateien nicht committet werden

- **Lizenz:** Der AcoustID-Datenbestand steht unter
  [CC BY-SA 3.0 Unported](https://creativecommons.org/licenses/by-sa/3.0/)
  (das MusicBrainz-AcoustID-Mapping ist Public Domain / CC0). Dieses Repo
  verteilt den Datenbestand bewusst **nicht weiter** — Weiterverteilung ist
  laut ARCHITECTURE.md §11 ausdrücklich ausgeschlossen und bleibt Sache der
  jeweiligen Instanz-Betreiber.
- **Größe:** Allein die Fingerprint-Datei eines einzigen Tages ist ~8,5 MB gz;
  Binärdaten gehören nicht in die Repo-Historie.

`.gitignore` schließt daher `tests/fixtures/acoustid-dumps/*.jsonl.gz` aus.
Diese README und das Skript bleiben versioniert, damit die Fixtures jederzeit
reproduzierbar beschafft werden können.

## Beschaffung

```bash
uv run python tests/fixtures/fetch_fixtures.py
# oder ohne uv:
python3 tests/fixtures/fetch_fixtures.py
```

Das Skript (nur Python-Stdlib) lädt genau die unten gelisteten Dateien von
`https://data.acoustid.org/2026/2026-07/`, überspringt bereits vorhandene
Dateien und prüft Größe und gzip-Integrität. Optionen: `--force` (erneut
laden), `--dest DIR` (anderes Zielverzeichnis), `--list` (nur auflisten).

## Inhalt

| Datei | Größe (Byte) | Zweck |
|---|---|---|
| `2026-07-22-fingerprint-update.jsonl.gz` | 8.925.088 | Fingerprint-Vektoren (int32-Arrays), größter Strom |
| `2026-07-22-meta-update.jsonl.gz` | 169.027 | Metadaten inkl. Sonderzeichen/Quotes (JSONL-Validität belegt) |
| `2026-07-22-track-update.jsonl.gz` | 53.159 | Tracks inkl. `new_id`-Merges |
| `2026-07-22-track_fingerprint-update.jsonl.gz` | 35.820 | zweite Projektion derselben Upstream-Tabelle |
| `2026-07-22-track_mbid-update.jsonl.gz` | 37.387 | Track→MBID-Zuordnungen |
| `2026-07-22-track_meta-update.jsonl.gz` | 122.663 | Track→Meta-Zuordnungen |
| `2026-07-22-track_puid-update.jsonl.gz` | 23 | Edge Case: legitime **leere** Datei (Legacy-Strom) |
| `2026-07-23-fingerprint-update.jsonl.gz` | 23 | Edge Case: leerer Tag im größten Strom (Daten-Flaute) |
| `2026-07-23-track_mbid-update.jsonl.gz` | 316 | Edge Case: Minimal-Delta, u. a. `disabled: true` |

Der 22.07.2026 ist als vollständiger Tag (alle sieben Ströme) enthalten, der
23.07.2026 liefert die Randfälle. Die Dateien sind upstream unveränderlich;
die Größen dienen dem Skript als Plausibilitätsprüfung.

## Quelle und Nutzungshinweis

Quelle: <https://data.acoustid.org/> (AcoustID OÜ). Beim Nachladen bitte
sparsam bleiben — die Dateien einmal holen und liegen lassen, nicht in
Schleifen ziehen.
