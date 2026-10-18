import csv
import io

import pymupdf

from lnd.gateway import LLMGateway
from lnd.worker import make_queue

from .conftest import StubSandbox, make_pdf, make_png
from .helpers import drain, upload, upload_image


def finish(api, model, sandbox=None):
    drain(make_queue(), LLMGateway(base_url=model.url), sandbox or StubSandbox())


def first_page(api, doc_id, tenant="default"):
    return api.get(f"/api/documents/{doc_id}", headers={"X-Tenant": tenant}).json()["pages"][0]


def test_healthz(clean, api):
    assert api.get("/healthz").json() == {"status": "ok"}


def test_rejects_files_that_are_not_pdf_or_image(clean, api):
    res = api.post("/api/documents", files={"file": ("notes.txt", b"just some text")})
    assert res.status_code == 400
    assert "PDF, PNG or JPEG" in res.json()["detail"]
    res = api.post("/api/documents", files={"file": ("bad.pdf", b"%PDF-1.4 not really")})
    assert res.status_code == 400
    assert api.get("/api/documents").json() == []


def test_rejects_password_protected_pdf(clean, api):
    doc = pymupdf.open()
    doc.new_page()
    protected = doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner"
    )
    res = api.post("/api/documents", files={"file": ("locked.pdf", protected)})
    assert res.status_code == 400 and "password" in res.json()["detail"]


def test_upload_counts_pdf_pages_without_rendering_them(clean, api):
    doc = upload(api, make_pdf(25), "big.pdf")
    assert doc["page_count"] == 25 and doc["kind"] == "pdf"
    assert make_queue().stats()["ready"] == 25
    detail = api.get(f"/api/documents/{doc['id']}").json()
    assert detail["pages_by_status"]["queued"] == 25
    # No page images exist until a worker renders them.
    assert api.get(f"/api/pages/{detail['pages'][0]['id']}/image").status_code == 404


def test_unknown_ids_are_404_not_500(clean, api):
    assert api.get("/api/documents/not-a-uuid").status_code == 404
    assert api.get("/api/documents/00000000-0000-0000-0000-000000000000").status_code == 404
    assert api.get("/api/pages/00000000-0000-0000-0000-000000000000/record").status_code == 404


def test_tenants_cannot_see_each_others_documents(clean, api, model):
    doc = upload_image(api, tenant="alice")
    finish(api, model)
    assert api.get("/api/documents", headers={"X-Tenant": "alice"}).json()[0]["id"] == doc["id"]
    assert api.get("/api/documents", headers={"X-Tenant": "bob"}).json() == []
    page = first_page(api, doc["id"], "alice")
    for path in (
        f"/api/documents/{doc['id']}",
        f"/api/documents/{doc['id']}/export.csv",
        f"/api/pages/{page['id']}/record",
        f"/api/pages/{page['id']}/image",
    ):
        assert api.get(path, headers={"X-Tenant": "bob"}).status_code == 404, path
    # a browser can pass the tenant in the query string
    assert api.get(f"/api/pages/{page['id']}/image?tenant=alice").status_code == 200


def test_bad_tenant_name_is_rejected(clean, api):
    assert api.get("/api/documents", headers={"X-Tenant": "a b/../c"}).status_code == 400


def test_editing_saves_a_new_version_and_keeps_history(clean, api, model):
    doc = upload_image(api)
    finish(api, model)
    page = first_page(api, doc["id"])
    rec = api.get(f"/api/pages/{page['id']}/record").json()["record"]["data"]

    rec["title"] = "Aspirin, corrected"
    rec["reagents"][1]["amount"] = 4.5
    res = api.put(
        f"/api/pages/{page['id']}/record",
        json={"data": rec, "editor": "dana", "note": "fixed volume"},
    )
    assert res.status_code == 200 and res.json()["version"] == 2

    rec["notes"] = "second edit"
    assert (
        api.put(f"/api/pages/{page['id']}/record", json={"data": rec, "editor": "dana"}).json()[
            "version"
        ]
        == 3
    )

    current = api.get(f"/api/pages/{page['id']}/record").json()["record"]
    assert current["version"] == 3 and current["data"]["notes"] == "second edit"

    versions = api.get(f"/api/pages/{page['id']}/versions").json()
    assert [(v["version"], v["source"]) for v in versions] == [
        (3, "user"),
        (2, "user"),
        (1, "model"),
    ]
    assert versions[1]["editor"] == "dana" and versions[1]["note"] == "fixed volume"

    original = api.get(f"/api/pages/{page['id']}/versions/1").json()
    assert original["data"]["title"] == "Synthesis of aspirin"  # the model's answer is kept
    assert original["data"]["reagents"][1]["amount"] == 3.0


def test_edit_is_validated(clean, api, model):
    upload_image(api)
    finish(api, model)
    doc = api.get("/api/documents").json()[0]
    page = first_page(api, doc["id"])
    good = api.get(f"/api/pages/{page['id']}/record").json()["record"]["data"]
    for bad in (
        {**good, "date": "14 March"},
        {**good, "surprise": 1},
        {**good, "reagents": [{"name": "", "amount": 1}]},
    ):
        res = api.put(f"/api/pages/{page['id']}/record", json={"data": bad})
        assert res.status_code == 422
    assert len(api.get(f"/api/pages/{page['id']}/versions").json()) == 1


def test_cannot_edit_a_page_that_has_no_record_yet(clean, api):
    doc = upload_image(api)
    page = first_page(api, doc["id"])
    body = {
        "data": {"title": "x", "date": None, "narrative": "", "reagents": [], "notes": ""},
    }
    assert api.put(f"/api/pages/{page['id']}/record", json=body).status_code == 409


def test_editing_reruns_the_stored_check_on_the_new_rows(clean, api, model):
    upload_image(api)
    sandbox = StubSandbox(issues=["amounts do not match"])
    finish(api, model, sandbox)
    api.app.state.sandbox = sandbox
    doc = api.get("/api/documents").json()[0]
    page = first_page(api, doc["id"])
    data = api.get(f"/api/pages/{page['id']}/record").json()["record"]["data"]
    calls_before = len(sandbox.calls)
    data["reagents"][0]["amount"] = 9.9
    res = api.put(f"/api/pages/{page['id']}/record", json={"data": data}).json()
    assert len(sandbox.calls) == calls_before + 1
    assert sandbox.calls[-1][1][0]["amount"] == 9.9  # ran on the edited rows
    assert res["checks"]["issues"] == ["amounts do not match"]


def test_exports(clean, api, model):
    upload_image(api)
    finish(api, model)
    doc = api.get("/api/documents").json()[0]
    page = first_page(api, doc["id"])
    data = api.get(f"/api/pages/{page['id']}/record").json()["record"]["data"]
    data["title"] = '=HYPERLINK("http://evil")'
    data["reagents"][0]["name"] = "@SUM(A1)"
    data["reagents"][0]["amount"] = -2.5
    api.put(f"/api/pages/{page['id']}/record", json={"data": data, "editor": "x"})

    as_json = api.get(f"/api/documents/{doc['id']}/export.json").json()
    assert as_json["document"]["page_count"] == 1
    assert as_json["pages"][0]["version"] == 2  # exports the latest version
    assert as_json["pages"][0]["data"]["reagents"][0]["amount"] == -2.5

    res = api.get(f"/api/documents/{doc['id']}/export.csv")
    assert res.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(res.text)))
    assert len(rows) == 2  # one line per reagent
    assert rows[0]["title"].startswith("'=")  # formulas are defused
    assert rows[0]["reagent_name"].startswith("'@")
    assert rows[0]["amount"] == "-2.5"  # real numbers are left alone
    assert rows[1]["reagent_name"] == "Acetic anhydride"
    assert rows[0]["version"] == "2" and rows[0]["source"] == "user"


def test_csv_keeps_pages_that_have_no_reagents_or_no_result(clean, api, model):
    doc = upload(api, make_pdf(2), "two.pdf")
    page1 = api.get(f"/api/documents/{doc['id']}").json()["pages"][0]
    # page 1 finished with no reagents, page 2 still queued
    from lnd import store

    store.finalize_page_done(
        page_id=page1["id"],
        document_id=doc["id"],
        extraction={"title": "Blank", "date": None, "narrative": "", "reagents": [], "notes": ""},
        check_code=None,
        check_result=None,
    )
    rows = list(csv.DictReader(io.StringIO(api.get(f"/api/documents/{doc['id']}/export.csv").text)))
    assert [(r["page"], r["page_status"], r["title"]) for r in rows] == [
        ("1", "done", "Blank"),
        ("2", "queued", ""),
    ]


def test_photo_with_exif_rotation_is_straightened(clean, api):
    from PIL import Image

    img = Image.new("RGB", (300, 200), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    doc = upload(api, buf.getvalue(), "phone.jpg")
    page = api.get(f"/api/documents/{doc['id']}").json()["pages"][0]
    stored = Image.open(io.BytesIO(api.get(f"/api/pages/{page['id']}/image").content))
    assert stored.size == (200, 300)


def test_png_upload_is_stored_byte_for_byte(clean, api):
    raw = make_png("same bytes")
    doc = upload(api, raw, "p.png")
    page = api.get(f"/api/documents/{doc['id']}").json()["pages"][0]
    assert api.get(f"/api/pages/{page['id']}/image").content == raw


def test_queue_endpoint(clean, api):
    upload(api, make_pdf(3), "q.pdf")
    stats = api.get("/api/queue").json()
    assert stats["ready"] == 3 and stats["dead"] == 0 and stats["dead_jobs"] == []


def test_ui_is_served(api):
    res = api.get("/")
    assert res.status_code == 200 and "Lab Notebook Digitizer" in res.text
    assert api.get("/app.js").status_code == 200
