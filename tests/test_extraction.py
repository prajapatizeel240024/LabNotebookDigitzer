import json

import pytest

from lnd.db import connection
from lnd.extraction import ExtractionError, extract_page, parse_extraction
from lnd.fakes.model_server import CANNED_PAGE
from lnd.gateway import LLMGateway

from .conftest import make_png


def run(gw):
    return extract_page(gw, image=make_png(), mime="image/png", tenant="t", job_id="d:1")


def purposes():
    with connection() as conn:
        rows = conn.execute("SELECT purpose FROM llm_calls ORDER BY id").fetchall()
    return [r["purpose"] for r in rows]


def test_valid_answer_takes_one_call(clean, model):
    page = run(LLMGateway(base_url=model.url))
    assert page.title == "Synthesis of aspirin"
    assert page.reagents[0].name == "Salicylic acid"
    assert purposes() == ["extract"]


def test_invalid_answer_is_retried_once_and_then_accepted(clean, model):
    model.control(invalid_next=1)
    page = run(LLMGateway(base_url=model.url))
    assert page.date == "2025-03-14"
    assert purposes() == ["extract", "extract_retry"]


def test_two_invalid_answers_raise(clean, model):
    model.control(invalid_next=2)
    with pytest.raises(ExtractionError):
        run(LLMGateway(base_url=model.url))
    assert purposes() == ["extract", "extract_retry"]  # one retry, not more


def test_valid_json_that_breaks_the_schema_is_invalid():
    bad = dict(CANNED_PAGE, date="14 March 2025")
    with pytest.raises(ValueError):
        parse_extraction(json.dumps(bad))
