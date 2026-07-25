"""Fehlerhierarchie der MB-Query-Schicht (ARCHITECTURE §5.4).

Die MusicBrainz-Spiegel-Datenbank ist ein **fremder** Dienst: sie gehoert
nicht zu unserem Stack, wird nicht von uns gestartet und kann jederzeit weg
sein, ohne dass das ein Fehler unserer Instanz waere. Genau darum muss der
Aufrufer die vier Faelle auseinanderhalten koennen — sie fuehren zu
grundverschiedenen Antworten:

* :class:`MbUnavailable` — keine Verbindung, Pool leer, Frist abgelaufen
  oder der Circuit-Breaker ist offen. **Degradiert antworten** (HTTP 200
  mit AcoustID-UUIDs und MBIDs, ohne Metadaten; Invariante §8.7).
* :class:`MbSchemaMismatch` — der Spiegel hat ein anderes Schema, als
  :mod:`shared.mb.queries` erwartet (jaehrliche MB-Schema-Aenderung, kaputte
  Rechteverteilung, fehlende View). **Laut loggen, degradiert weiterlaufen**
  — eine Instanz, die wegen einer fehlenden Spalte keine Lookups mehr
  beantwortet, ist schaedlicher als eine ohne Metadaten.
* :class:`MbStale` — der Spiegel repliziert nicht mehr (Schwellen
  :data:`~shared.mb.client.STALE_WARN_HOURS` / ``STALE_CRIT_HOURS``). Nur
  Log und Metrik; die Daten sind ja noch da und werden weiter geliefert.
* :class:`MbQueryError` — eine Abfrage ist gescheitert, obwohl Verbindung
  und Schema stehen. Das ist **unser** Fehler: HTTP 500, nicht degradieren.
  Wer hier degradiert, verbirgt einen Programmfehler hinter leeren
  Metadaten.

**psycopg dringt nie nach aussen.** Jede Ausnahme des Treibers wird in eine
dieser Klassen uebersetzt (:func:`~shared.mb.client.translate_error`); der
API-Dienst kennt psycopg fuer die MB-Seite gar nicht.

Drei der vier Namen tragen bewusst **kein** ``Error``-Suffix (anders als
die Konvention, die ruffs N818 einfordert): sie sind so im Phase-1-Bericht
und in ARCHITECTURE §5.4 festgelegt und tauchen dort als Vertragsbegriffe
auf. Umbenennen wuerde die Doku von der Umsetzung trennen.
"""

from __future__ import annotations

__all__ = [
    "MbError",
    "MbQueryError",
    "MbSchemaMismatch",
    "MbStale",
    "MbUnavailable",
]


class MbError(Exception):
    """Basis aller Fehler der MB-Query-Schicht."""


class MbUnavailable(MbError):  # noqa: N818 — Name laut §5.4
    """Die MusicBrainz-Postgres ist gerade nicht ansprechbar.

    Verbindungsaufbau, Pool-Wartezeit, Netz oder der offene Circuit-Breaker.
    Der Lookup antwortet daraufhin **ohne** Metadaten mit HTTP 200
    (Invariante §8.7) und protokolliert das Ereignis.
    """


class MbSchemaMismatch(MbError):  # noqa: N818 — Name laut §5.4
    """Der Spiegel hat nicht die Spalten, die die Abfragen brauchen.

    Ergebnis des Selfchecks beim Start (:meth:`~shared.mb.client.MbClient.
    startup_check`). Erwartet werden nur die Spalten aus
    :data:`~shared.mb.queries.EXPECTED_COLUMNS`; **zusaetzliche** Spalten
    sind kein Mismatch — die MB-Schema-Aenderungen der letzten Jahre waren
    rein additiv, und genau deshalb stehen ueberall explizite Spaltenlisten.

    Attributes:
        missing: Fehlende Spalten als ``"tabelle.spalte"``, sortiert.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            "MusicBrainz-Schema passt nicht zur Erwartung; es fehlen: " + ", ".join(missing)
        )


class MbStale(MbError):  # noqa: N818 — Name laut §5.4
    """Der Spiegel hinkt zu weit hinterher.

    Wird nur fuer Log und Metrik gebildet und nie geworfen: veraltete
    Metadaten sind brauchbare Metadaten. Die Schwellen stehen in
    :mod:`shared.mb.client`.

    Attributes:
        age_hours: Alter der letzten Replikation in Stunden.
        critical: ``True`` ab der CRIT-Schwelle.
    """

    def __init__(self, age_hours: float, *, critical: bool) -> None:
        self.age_hours = age_hours
        self.critical = critical
        level = "kritisch" if critical else "auffaellig"
        super().__init__(
            f"MusicBrainz-Spiegel ist {level} veraltet: letzte Replikation vor "
            f"{age_hours:.1f} Stunden"
        )


class MbQueryError(MbError):
    """Eine Abfrage ist gescheitert, obwohl die Verbindung stand.

    Syntaxfehler, Typfehler, ueberschrittenes ``statement_timeout``,
    verweigerte Rechte. Der Lookup wird dadurch zu Fehler 5 / HTTP 500 —
    bewusst **kein** degradierter Betrieb, sonst verschwindet ein
    Programmfehler in leeren Metadaten.
    """
