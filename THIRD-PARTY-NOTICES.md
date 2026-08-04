# THIRD-PARTY-NOTICES

Das Auslieferungs-Image dieses Projekts ist eine **Zusammenstellung**: neben
dem eigenen Code (MIT, siehe `LICENSE`) enthält es fremde Programme, die
unter ihren eigenen Lizenzen stehen. Diese Datei nennt sie, ihre Lizenz und
den Weg zum Quelltext (Entscheid E7, DECISIONS 2026-08-04).

Die eingebackenen Programme laufen als **eigenständige Prozesse** und
sprechen ausschließlich über Netzwerkprotokolle (HTTP, das Postgres-Protokoll)
mit dem eigenen Code — es entsteht kein abgeleitetes Werk. Die **Weitergabe**
der Binaries erzeugt trotzdem Pflichten; ihnen wird hier nachgekommen.

---

## acoustid-index (`fpindex`)

| | |
|---|---|
| Zweck | Fingerprint-Suchindex (ARCHITECTURE §5.3) |
| Lizenz | **GPL-3.0-or-later** |
| Projekt | <https://github.com/acoustid/acoustid-index> |
| Gebauter Stand | Commit `6bc929a316e4f3a9c9ec37a395f30e0f5b7116c2` (2025-10-27) |
| Im Image | `/usr/local/bin/fpindex` |

**Quellangebot.** Der vollständige Quelltext des ausgelieferten Binaries
liegt im Image selbst:

```
/usr/share/musicmeta/acoustid-index-source.tar.gz
```

Ihn herausholen (ohne den Container zu starten):

```
docker run --rm --entrypoint cat <image> \
  /usr/share/musicmeta/acoustid-index-source.tar.gz > acoustid-index-source.tar.gz
```

Derselbe Stand ist über das Projekt-Repository beziehbar:

```
git clone https://github.com/acoustid/acoustid-index.git
git -C acoustid-index checkout 6bc929a316e4f3a9c9ec37a395f30e0f5b7116c2
```

**Bauanleitung** (identisch mit der Stufe `fpindex-build` im `Dockerfile`):
Zig 0.14.0, dann `zig build --release=fast`; das Ergebnis liegt unter
`zig-out/bin/fpindex`.

Der Commit steht außerdem als Label am Image
(`org.musicmeta.acoustid-index.commit`) und ist damit ohne diese Datei
prüfbar:

```
docker inspect --format '{{ index .Config.Labels "org.musicmeta.acoustid-index.commit" }}' <image>
```

**Nicht enthalten** ist die Postgres-Erweiterung `pg_acoustid`: sie hat keine
Lizenzangabe und wird ausschließlich in der CI als Referenz für die
Bit-Verifikation gebaut (DECISIONS 2026-07-25), nie ausgeliefert.

---

## PostgreSQL

| | |
|---|---|
| Zweck | Datenbank des AcoustID-Bestands |
| Lizenz | PostgreSQL License (BSD-artig) |
| Projekt | <https://www.postgresql.org/> |
| Bezug | Binärpakete der PostgreSQL Global Development Group (`apt.postgresql.org`), Major-Version im Label `org.musicmeta.postgresql.major` |

---

## supervisord

| | |
|---|---|
| Zweck | Prozess-Supervisor der Dauerdienste (E1) |
| Lizenz | BSD-artig (Repoze Public License) |
| Projekt | <https://github.com/Supervisor/supervisor> |
| Im Image | eigenes venv unter `/opt/supervisor` |

---

## tini

| | |
|---|---|
| Zweck | PID 1: Zombie-Reaping und Signalweiterleitung |
| Lizenz | MIT |
| Projekt | <https://github.com/krallin/tini> |

---

## Python-Abhängigkeiten

Die Python-Pakete der Anwendung sind in `uv.lock` mit Version und Hash
festgehalten; ihre Lizenzen sind die der jeweiligen Projekte (überwiegend
MIT, BSD, Apache-2.0). Eine Aufstellung erzeugt

```
uv pip list --format=json
```

im laufenden Container (`/app/.venv`).

---

## Debian-Basissystem

Das Laufzeit-Image basiert auf `python:3.14-slim-bookworm` (Debian 12). Die
Lizenzen der enthaltenen Pakete liegen im Image unter
`/usr/share/doc/<paket>/copyright`.
