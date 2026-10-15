"""End-to-end tests through the Flask app and the demo statements."""
import io
import os
import zipfile

import pytest

import app as app_module
from demo.make_demo import fy25, fy26


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _upload(client, **form):
    data = {"files": [(io.BytesIO(fy25.encode()), "demo_fy2025.csv"),
                      (io.BytesIO(fy26.encode()), "demo_fy2026.csv")]}
    data.update(form)
    return client.post("/api/analyze", data=data, content_type="multipart/form-data")


def test_analyze_demo_end_to_end(client):
    r = _upload(client, entity="individual")
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    s = d["summary"]
    # snapshot of the demo scenario (verified by hand):
    assert s["net_capital_gain_18A"] == 2842.74
    assert s["total_capital_gains_18H"] == 6534.95
    assert s["d2_open_written"] == 828.62
    assert s["closed_net"] == 4782.45
    assert d["fy_detected"] == 2026
    assert d["tables"]["closed_lots"]["total"] == 4
    assert d["tables"]["transfers"]["total"] == 1
    assert d["reconciliation"]["rows_ok"] == d["reconciliation"]["rows_checked"] == 5
    # the demo NVDA put is open at 30 June and the files stop there
    assert s["d2_outcome_unknown"] == 828.62
    assert d["warnings"][0].startswith("D2 OUTCOME NOT IN UPLOADED DATA")
    assert "NVDA 17JUL26 100 P" in d["warnings"][0]

    # downloads
    for kind, sniff in (("csv", b"IBKR AUSTRALIAN CGT WORKPAPER"),
                        ("pdf", b"%PDF"), ("zip", b"PK")):
        resp = client.get(d["downloads"][kind])
        assert resp.status_code == 200
        assert resp.data[:80].startswith(sniff) or sniff in resp.data[:200]

    z = zipfile.ZipFile(io.BytesIO(client.get(d["downloads"]["zip"]).data))
    names = z.namelist()
    assert any(n.endswith("_workpaper.csv") for n in names)
    assert any(n.endswith("_report.pdf") for n in names)
    assert any(n.endswith("_summary.json") for n in names)


def test_analyze_rejects_junk(client):
    r = client.post("/api/analyze",
                    data={"files": (io.BytesIO(b"name,age\nbob,2\n"), "junk.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 422
    assert "IBKR" in r.get_json()["error"]


def _post_one(client, content, name):
    return client.post("/api/analyze", data={"files": (io.BytesIO(content), name)},
                       content_type="multipart/form-data")


@pytest.mark.parametrize("content,name,kind", [
    (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\r<< /Type /Catalog >>\rendobj\n", "U9999999.pdf",
     "is a PDF"),
    # renamed: the content decides, not the extension
    (b"%PDF-1.4\n1 0 obj\r<<>>\rendobj\n", "U9999999.csv", "is a PDF"),
    (b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00!\x00", "statement.xlsx", "Excel workbook"),
    (b"PK\x03\x04\x14\x00\x00\x00\x08\x00", "statements.zip", "extract them"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1\x00\x00", "statement.xls", "legacy Excel"),
    (b"<!DOCTYPE html><html><body>Activity Statement</body></html>", "statement.htm",
     "HTML page"),
])
def test_non_csv_upload_gets_a_clear_message(client, content, name, kind):
    r = _post_one(client, content, name)
    assert r.status_code == 422
    err = r.get_json()["error"]
    assert kind in err
    assert name in err
    assert "CSV as the format" in err                 # says what to do instead
    assert "new-line character" not in err


def test_malformed_csv_error_is_wrapped(client):
    # a bare carriage return inside a field: csv raises "new-line character seen
    # in unquoted field", which on its own tells the user nothing
    bad = (b"Statement,Header,Field Name,Field Value\n"
           b"Statement,Data,Title,Activity\rStatement\n")
    r = _post_one(client, bad, "statement.csv")
    assert r.status_code == 422
    err = r.get_json()["error"]
    assert err.startswith("statement.csv could not be read as a CSV file (line")
    assert "re-saving it in Excel" in err and "CSV as the format" in err


def test_analyze_requires_files(client):
    r = client.post("/api/analyze", data={}, content_type="multipart/form-data")
    assert r.status_code == 400


def test_invalid_entity_rejected(client):
    r = _upload(client, entity="wizard")
    assert r.status_code == 422


def test_negative_carried_losses_rejected(client):
    r = _upload(client, carried_losses="-5")
    assert r.status_code == 400


def test_download_token_traversal_blocked(client):
    assert client.get("/api/download/../../etc/csv").status_code in (404, 308)
    assert client.get("/api/download/nonexistenttoken123/csv").status_code == 404


def test_pdf_disclaimer_is_english_only(client):
    d = _upload(client).get_json()
    pdf = client.get(d["downloads"]["pdf"]).data
    assert pdf.startswith(b"%PDF")
    # the Chinese paragraph needed reportlab's STSong-Light CID font
    assert b"STSong" not in pdf and b"UniGB" not in pdf
