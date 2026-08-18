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
