"""Serialisierung: json, jsonp, xml — Zeichen fuer Zeichen wie im Original."""

from __future__ import annotations

import json

import pytest
from original_examples import LOOKUP_OK_EXAMPLE

from acoustid_api.formats import DEFAULT_CALLBACK, ResponseFormat, singular

# --- Auswahl ---------------------------------------------------------------


def test_default_is_json() -> None:
    assert ResponseFormat.parse(None, None).name == "json"
    assert ResponseFormat.parse(None, None).callback is None


@pytest.mark.parametrize("name", ["json", "jsonp", "xml"])
def test_the_three_documented_formats_are_accepted(name: str) -> None:
    assert ResponseFormat.parse(name, None).name == name


@pytest.mark.parametrize("name", ["", "yaml", "JSON", "msgpack"])
def test_unknown_format_is_rejected_with_its_own_name(name: str) -> None:
    with pytest.raises(ValueError, match="^" + (name or "") + "$"):
        ResponseFormat.parse(name, None)


def test_jsonp_callback_defaults_and_is_validated() -> None:
    assert ResponseFormat.parse("jsonp", None).callback == DEFAULT_CALLBACK
    assert ResponseFormat.parse("jsonp", "myCallback").callback == "myCallback"
    assert ResponseFormat.parse("jsonp", "window.cb").callback == "window.cb"
    # Kein Fehler, sondern der Rueckfallname — genau wie im Original.
    assert ResponseFormat.parse("jsonp", "alert(1)").callback == DEFAULT_CALLBACK
    assert ResponseFormat.parse("jsonp", "1nvalid").callback == DEFAULT_CALLBACK


# --- Ausgabe ---------------------------------------------------------------


def test_json_sorts_keys_and_reproduces_the_original_example() -> None:
    rendered = ResponseFormat.parse("json", None).render(LOOKUP_OK_EXAMPLE)
    assert rendered.content_type == "application/json; charset=UTF-8"
    assert json.loads(rendered.body) == LOOKUP_OK_EXAMPLE
    # sort_keys=True: `results` steht vor `status`.
    assert rendered.body.index('"results"') < rendered.body.index('"status"')


def test_jsonp_wraps_the_same_json() -> None:
    rendered = ResponseFormat.parse("jsonp", "cb").render(LOOKUP_OK_EXAMPLE)
    assert rendered.content_type == "application/javascript; charset=UTF-8"
    assert rendered.body.startswith("cb(")
    assert rendered.body.endswith(")")
    assert json.loads(rendered.body[3:-1]) == LOOKUP_OK_EXAMPLE


def test_xml_has_the_original_declaration_and_singular_items() -> None:
    rendered = ResponseFormat.parse("xml", None).render(LOOKUP_OK_EXAMPLE)
    assert rendered.content_type == "text/xml; charset=UTF-8"
    assert rendered.body.startswith("<?xml version='1.0' encoding='UTF-8'?>")
    assert "<results><result>" in rendered.body
    assert "<id>&lt;track-gid&gt;</id>" in rendered.body
    assert "<status>ok</status>" in rendered.body


def test_xml_sorts_keys_too() -> None:
    body = ResponseFormat.parse("xml", None).render({"status": "ok", "results": []}).body
    assert body.index("<results") < body.index("<status")


def test_xml_turns_at_keys_into_attributes() -> None:
    body = ResponseFormat.parse("xml", None).render({"item": {"@id": 7, "name": "x"}}).body
    assert '<item id="7">' in body


@pytest.mark.parametrize(
    ("plural", "expected"),
    [("results", "result"), ("fingerprints", "fingerprint"), ("secondarytypes", "secondarytype")],
)
def test_singular_forms_used_by_the_response(plural: str, expected: str) -> None:
    assert singular(plural) == expected


def test_singular_refuses_names_that_are_no_plural() -> None:
    """Ein fehlender Name in der Antwortstruktur soll auffallen, nicht raten."""
    with pytest.raises(ValueError, match="Pluralform"):
        singular("result")
