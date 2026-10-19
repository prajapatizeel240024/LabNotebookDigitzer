import json

import pytest
from pydantic import ValidationError

from lnd.extraction import ExtractionError, parse_extraction
from lnd.schemas import PageExtraction, llm_json_schema

GOOD = {
    "title": "Aldol condensation",
    "date": "2025-02-03",
    "narrative": "Stirred overnight.",
    "reagents": [
        {
            "name": "Benzaldehyde",
            "amount": 1.06,
            "unit": "g",
            "molar_mass_g_mol": 106.12,
            "equivalents": 1.0,
        }
    ],
    "notes": "",
}


def test_valid_page_round_trips():
    page = PageExtraction.model_validate(GOOD)
    assert page.model_dump() == GOOD


def test_extra_keys_are_rejected():
    with pytest.raises(ValidationError):
        PageExtraction.model_validate({**GOOD, "confidence": 0.9})
    bad_reagent = {**GOOD, "reagents": [{**GOOD["reagents"][0], "cas": "100-52-7"}]}
    with pytest.raises(ValidationError):
        PageExtraction.model_validate(bad_reagent)


@pytest.mark.parametrize("bad", ["14 March 2025", "2025/03/14", "2025-3-4", "yesterday"])
def test_date_must_be_iso_or_null(bad):
    with pytest.raises(ValidationError):
        PageExtraction.model_validate({**GOOD, "date": bad})


def test_null_and_empty_date_become_none():
    assert PageExtraction.model_validate({**GOOD, "date": None}).date is None
    assert PageExtraction.model_validate({**GOOD, "date": ""}).date is None


def test_blank_cells_can_be_null():
    reagent = {
        "name": "Water",
        "amount": None,
        "unit": None,
        "molar_mass_g_mol": None,
        "equivalents": None,
    }
    assert PageExtraction.model_validate({**GOOD, "reagents": [reagent]}).reagents[0].amount is None


def test_negative_molar_mass_rejected():
    reagent = {**GOOD["reagents"][0], "molar_mass_g_mol": -5}
    with pytest.raises(ValidationError):
        PageExtraction.model_validate({**GOOD, "reagents": [reagent]})


def test_llm_schema_is_flat_and_serialisable():
    schema = llm_json_schema()
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    assert set(schema["properties"]) == {"title", "date", "narrative", "reagents", "notes"}
    assert schema["additionalProperties"] is False
    item = schema["properties"]["reagents"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["properties"]) == {
        "name",
        "amount",
        "unit",
        "molar_mass_g_mol",
        "equivalents",
    }


def test_llm_schema_requires_every_key_in_page_order():
    # Optional keys let the model skip them: with only title and narrative required it never
    # wrote the date. Values can still be null; the keys must be there.
    schema = llm_json_schema()
    assert schema["required"] == ["title", "date", "narrative", "reagents", "notes"]
    item = schema["properties"]["reagents"]["items"]
    assert item["required"] == ["name", "amount", "unit", "molar_mass_g_mol", "equivalents"]


def test_parse_accepts_fenced_json():
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    assert parse_extraction(fenced).title == "Aldol condensation"


def test_parse_rejects_truncated_json():
    with pytest.raises(ValueError):
        parse_extraction('{"title": "x", "date"')


def test_extraction_error_is_an_exception():
    assert issubclass(ExtractionError, Exception)
