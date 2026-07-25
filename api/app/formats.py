"""Antwortformate der Web-API: json, jsonp, xml (Phase-1-Vertrag).

Der Parameter ``format`` waehlt, wie derselbe Datenbaum herausgeht. Alle drei
Formate stammen aus ``acoustid/api/__init__.py`` des Original-Servers und
sind bewusst zeichengenau nachgebaut — inklusive der Kleinigkeiten, die man
sonst uebersieht:

* **JSON mit sortierten Schluesseln** (``sort_keys=True``). In der Antwort
  steht deshalb ``results`` vor ``status``.
* **Content-Types** mit grossgeschriebenem ``charset=UTF-8``:
  ``application/json``, ``application/javascript`` (jsonp), ``text/xml``.
* **jsonp** verpackt dasselbe JSON in einen Funktionsaufruf. Der Name kommt
  aus ``jsoncallback``; sieht er nicht wie ein JavaScript-Bezeichner aus,
  wird stillschweigend :data:`DEFAULT_CALLBACK` benutzt (kein Fehler — genau
  wie im Original).
* **XML** kennt keine anonymen Listen: aus ``results`` werden
  ``<results><result>…</result></results>``. Der Elementname des Kindes
  entsteht aus dem Singular des Plurals (:func:`singular`). Schluessel mit
  ``@`` werden Attribute.

Der Aufrufer bekommt kein fertiges ``Response``-Objekt, sondern
:class:`RenderedResponse` (Text + Content-Type) — so bleibt dieses Modul
frei von Framework-Details und ist ohne HTTP testbar.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Final, Self

__all__ = [
    "DEFAULT_CALLBACK",
    "DEFAULT_FORMAT",
    "FORMATS",
    "RenderedResponse",
    "ResponseFormat",
    "singular",
]

#: Ohne ``format``-Parameter wird JSON geliefert.
DEFAULT_FORMAT: Final = "json"

#: Die drei zulaessigen Werte von ``format``.
FORMATS: Final = frozenset({"json", "jsonp", "xml"})

#: Rueckfallname der jsonp-Funktion (Original-Default).
DEFAULT_CALLBACK: Final = "jsonAcoustidApi"

#: JavaScript-Bezeichner, optional mit Punkten (``window.foo.bar``).
_CALLBACK_RE: Final = re.compile(r"^[$A-Za-z_][0-9A-Za-z_]*(\.[$A-Za-z_][0-9A-Za-z_]*)*$")


def singular(plural: str) -> str:
    """Plural -> Singular fuer XML-Listenelemente (Original: ``singular``).

    Kennt genau so viel Englisch, wie die Antwortstruktur braucht.

    Raises:
        ValueError: Der Name sieht nicht nach Plural aus — dann fehlt in der
            Antwortstruktur ein Name, und das soll auffallen.
    """
    if plural.endswith("ies"):
        return plural[:-3] + "y"
    if plural.endswith("s"):
        return plural[:-1]
    raise ValueError(f"unbekannte Pluralform {plural!r}")


@dataclass(frozen=True, slots=True)
class RenderedResponse:
    """Fertig serialisierte Antwort ohne HTTP-Bindung."""

    body: str
    content_type: str


@dataclass(frozen=True, slots=True)
class ResponseFormat:
    """Das ausgewaehlte Antwortformat.

    Attributes:
        name: ``json``, ``jsonp`` oder ``xml``.
        callback: Nur bei ``jsonp`` gesetzt.
    """

    name: str = DEFAULT_FORMAT
    callback: str | None = None

    @classmethod
    def parse(cls, name: str | None, callback: str | None) -> Self:
        """Baut das Format aus ``format`` und ``jsoncallback``.

        Raises:
            ValueError: ``name`` ist keiner der drei zulaessigen Werte. Der
                Aufrufer macht daraus den Fehler 1 — und antwortet dabei in
                JSON, weil das gewuenschte Format ja unbekannt ist.
        """
        chosen = name if name is not None else DEFAULT_FORMAT
        if chosen not in FORMATS:
            raise ValueError(chosen)
        if chosen != "jsonp":
            return cls(name=chosen)
        wanted = callback or DEFAULT_CALLBACK
        if not _CALLBACK_RE.match(wanted):
            wanted = DEFAULT_CALLBACK
        return cls(name=chosen, callback=wanted)

    def render(self, data: dict[str, Any]) -> RenderedResponse:
        """Serialisiert den Antwortbaum in diesem Format."""
        if self.name == "xml":
            return _render_xml(data)
        body = json.dumps(data, sort_keys=True)
        if self.callback is None:
            return RenderedResponse(body, "application/json; charset=UTF-8")
        return RenderedResponse(f"{self.callback}({body})", "application/javascript; charset=UTF-8")


def _render_xml(data: dict[str, Any]) -> RenderedResponse:
    root = ElementTree.Element("response")
    _fill(root, data)
    # Bewusst ueber einen Bytes-Puffer: nur so steht `encoding='UTF-8'` in der
    # XML-Deklaration, genau wie beim Original (`tree.write(res,
    # encoding="UTF-8", xml_declaration=True)`).
    buffer = BytesIO()
    ElementTree.ElementTree(root).write(buffer, encoding="UTF-8", xml_declaration=True)
    return RenderedResponse(buffer.getvalue().decode("utf-8"), "text/xml; charset=UTF-8")


def _fill(parent: ElementTree.Element, data: Any) -> None:
    if isinstance(data, dict):
        for name, value in sorted(data.items()):
            if name.startswith("@"):
                parent.attrib[name[1:]] = str(value)
            else:
                _fill(ElementTree.SubElement(parent, name), value)
    elif isinstance(data, list):
        name = singular(parent.tag)
        for item in data:
            _fill(ElementTree.SubElement(parent, name), item)
    else:
        parent.text = str(data)
