"""Hosted-mode behaviour: page text, result retention, demo, limits."""
import io
import logging
import os
import time

import pytest
from werkzeug.middleware.proxy_fix import ProxyFix

import app as app_module
from app import RateLimiter, Settings
from demo.make_demo import fy25, fy26


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(app_module.SETTINGS, "hosted", True)
    monkeypatch.setattr(app_module.SETTINGS, "result_ttl", 15 * 60)


def _demo(client, ip="203.0.113.5", **form):
    return client.post("/api/demo", data=form, environ_base={"REMOTE_ADDR": ip})


def _run_dir(token):
    return os.path.join(app_module.SETTINGS.results_dir, token)


# ---------------------------------------------------------------- page text

SCOPE = ("Not for traders", "Parcels bought before 2023 are not supported",
         "USD base-currency", "AUD base-currency accounts are untested")


def test_index_local_mode(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "All processing happens locally on this machine — no data leaves it." in html
    assert 'id="privacy-local"' in html and 'id="privacy-hosted"' not in html
    assert "Sent over HTTPS" not in html
    for s in SCOPE:
        assert s in html
    assert "v0.1.0" in html
    assert "https://github.com/chrisudf/ibkr-tax-report/issues" in html
    assert "Strict-Transport-Security" not in r.headers
    assert not any("一" <= ch <= "鿿" for ch in html)   # English-only page


def test_index_hosted_mode(client, hosted):
    r = client.get("/")
    html = r.get_data(as_text=True)
    # the local claim would be false on a server
    assert "no data leaves it" not in html
    assert 'id="privacy-hosted"' in html
    for s in ("Sent over HTTPS to this server and processed in memory",
              "never written to disk",
              "kept for at most 15 minutes",
              "Delete my results now",
              "No logging of file contents, no analytics, no tracking, no cookies",
              "https://github.com/chrisudf/ibkr-tax-report#run",
              "python app.py"):
        assert s in html, s
    for s in SCOPE:
        assert s in html
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")


def test_index_shows_ttl_build_and_rba_coverage(client, monkeypatch):
    monkeypatch.setattr(app_module.SETTINGS, "result_ttl", 5 * 60)
    monkeypatch.setattr(app_module.SETTINGS, "build", "abc1234")
    html = client.get("/").get_data(as_text=True)
    assert 'data-ttl-minutes="5"' in html
    assert "v0.1.0 (abc1234)" in html
    first, last = app_module.get_fx().coverage("USD")
    assert f"RBA rates {first.day} {first:%b %Y} – {last.day} {last:%b %Y}" in html


def test_security_headers(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cache-Control"] == "no-store"
    assert _demo(client).headers["Cache-Control"] == "no-store"


def test_healthz(client):
    d = client.get("/healthz").get_json()
    assert d["ok"] is True and d["version"].startswith("v")
    assert d["rba"]["first"] <= d["rba"]["last"]


# ---------------------------------------------------------------- settings


def test_settings_defaults_and_env():
    s = Settings.from_env({})
    assert (s.hosted, s.result_ttl, s.max_upload_mb, s.rate_limits, s.proxy_hops) == \
        (False, 900, 10, [], 0)
    s = Settings.from_env({"IBKR_TAX_HOSTED": "1"})
    assert s.rate_limits == [(10, 60), (60, 3600)]     # hosted is never unlimited by default
    s = Settings.from_env({"IBKR_TAX_HOSTED": "1", "IBKR_TAX_RATE_LIMIT": ""})
    assert s.rate_limits == []                         # ... unless explicitly switched off
    s = Settings.from_env({"IBKR_TAX_RESULTS_DIR": "/results", "IBKR_TAX_RESULT_TTL_MINUTES": "5",
                           "IBKR_TAX_MAX_UPLOAD_MB": "3", "IBKR_TAX_RATE_LIMIT": "4/30",
                           "IBKR_TAX_PROXY_HOPS": "1", "IBKR_TAX_RBA_DIR": "/rba",
                           "IBKR_TAX_RBA_REFRESH_HOURS": "168", "IBKR_TAX_BUILD": "abc"})
    assert (s.results_dir, s.result_ttl, s.max_upload_mb, s.rate_limits, s.proxy_hops,
            s.rba_dir, s.rba_refresh_hours, s.build) == \
        ("/results", 300, 3, [(4, 30)], 1, "/rba", 168.0, "abc")
    with pytest.raises(ValueError):
        Settings.from_env({"IBKR_TAX_RATE_LIMIT": "0/60"})


# ---------------------------------------------------------------- demo


def test_demo_runs_bundled_files_without_upload(client):
    r = _demo(client)
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["demo"] is True
    assert d["meta"]["account"] == "U9999999"
    s = d["summary"]
    # same snapshot as uploading the two demo files (test_app.py)
    assert s["net_capital_gain_18A"] == 2842.74
    assert s["total_capital_gains_18H"] == 6534.95
    assert d["fy_detected"] == 2026
    assert d["warnings"][0].startswith("D2 OUTCOME NOT IN UPLOADED DATA")
    for kind, sniff in (("csv", b"IBKR AUSTRALIAN CGT WORKPAPER"), ("pdf", b"%PDF"),
                        ("zip", b"PK")):
        resp = client.get(d["downloads"][kind])
        assert resp.status_code == 200 and sniff in resp.data[:200]
    assert d["expires_in"] == app_module.SETTINGS.result_ttl
    assert d["delete_url"] == f"/api/result/{d['token']}"


def test_demo_matches_upload_of_the_same_files(client):
    up = client.post("/api/analyze", data={"files": [
        (io.BytesIO(fy25.encode()), "demo_fy2025.csv"),
        (io.BytesIO(fy26.encode()), "demo_fy2026.csv")], "entity": "smsf"},
        content_type="multipart/form-data").get_json()
    dm = _demo(client, entity="smsf").get_json()
    assert up["summary"] == dm["summary"]
    assert dm["summary"]["entity"] == "smsf"
    assert up["demo"] is False


def test_demo_files_on_disk_are_the_generated_ones():
    # make_demo.py writes demo/*.csv; the endpoint serves the files, the snapshot
    # tests use the module strings — keep them identical
    for name, text in (("demo_fy2025.csv", fy25), ("demo_fy2026.csv", fy26)):
        with open(os.path.join(app_module.DEMO_DIR, name), encoding="utf-8") as fh:
            assert fh.read().replace("\r\n", "\n") == text


# ---------------------------------------------------------------- retention


def test_delete_result_removes_files(client):
    d = _demo(client).get_json()
    assert os.path.isdir(_run_dir(d["token"]))
    r = client.delete(d["delete_url"])
    assert r.status_code == 200 and r.get_json() == {"deleted": True}
    assert not os.path.exists(_run_dir(d["token"]))
    assert client.get(d["downloads"]["pdf"]).status_code == 404
    assert client.delete(d["delete_url"]).status_code == 404


@pytest.mark.parametrize("token", ["short", "..", "a" * 65, "bad%2Ftoken0123456789"])
def test_delete_rejects_bad_tokens(client, token):
    assert client.delete(f"/api/result/{token}").status_code in (404, 405)


def test_sweep_removes_only_expired_results(client):
    old = _demo(client).get_json()["token"]
    new = _demo(client).get_json()["token"]
    past = time.time() - app_module.SETTINGS.result_ttl - 1
    os.utime(_run_dir(old), (past, past))
    assert app_module.sweep_results() == 1
    assert not os.path.exists(_run_dir(old))
    assert os.path.isdir(_run_dir(new))


def test_expired_result_is_not_served_before_the_sweep(client):
    d = _demo(client).get_json()
    past = time.time() - app_module.SETTINGS.result_ttl - 1
    os.utime(_run_dir(d["token"]), (past, past))
    assert client.get(d["downloads"]["csv"]).status_code == 404
    assert not os.path.exists(_run_dir(d["token"]))


def test_background_sweeper_deletes_without_further_requests(client, monkeypatch):
    monkeypatch.setattr(app_module.SETTINGS, "result_ttl", 1)
    monkeypatch.setattr(app_module.SETTINGS, "rba_dir", "")
    token = _demo(client).get_json()["token"]
    app_module.start_background()
    app_module.start_background()          # idempotent
    assert [t.name for t in app_module._bg_threads] == ["result-sweeper"]
    deadline = time.time() + 6
    while os.path.exists(_run_dir(token)) and time.time() < deadline:
        time.sleep(0.1)
    assert not os.path.exists(_run_dir(token))
    assert os.listdir(app_module.SETTINGS.results_dir) == []


def test_results_dir_is_configurable(client, tmp_path, monkeypatch):
    target = tmp_path / "tmpfs"
    monkeypatch.setattr(app_module.SETTINGS, "results_dir", str(target))
    token = _demo(client).get_json()["token"]
    assert sorted(os.listdir(target / token)) == [
        "U9999999_FY2025-26_bundle.zip", "U9999999_FY2025-26_report.pdf",
        "U9999999_FY2025-26_workpaper.csv"]


def _upload_as(client, account):
    files = [(io.BytesIO(t.replace("U9999999", account).encode()), n)
             for t, n in ((fy25, "a.csv"), (fy26, "b.csv"))]
    return client.post("/api/analyze", data={"files": files},
                       content_type="multipart/form-data")


@pytest.mark.parametrize("account", ["../escape", "..\\escape", "/etc/escape", "ABS"])
def test_crafted_account_cannot_place_files_outside_the_run(client, tmp_path, account):
    # report file names come from the statement's Account field
    outside = tmp_path / "outside"
    if account == "ABS":
        account = str(outside / "x")          # an absolute path, harmless if it ever escaped
    r = _upload_as(client, account)
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    root = app_module.SETTINGS.results_dir
    assert os.listdir(root) == [d["token"]]
    names = os.listdir(os.path.join(root, d["token"]))
    assert len(names) == 3 and all(n.replace("-", "").replace("_", "").replace(".", "").isalnum()
                                   for n in names)
    assert not outside.exists()
    assert client.get(d["downloads"]["pdf"]).status_code == 200
    client.delete(d["delete_url"])
    assert os.listdir(root) == []             # deleting the run removes everything


def test_file_stem():
    assert app_module._file_stem("U9999999", "FY2025-26") == "U9999999_FY2025-26"
    assert app_module._file_stem("../../x", "FY2025-26") == "x_FY2025-26"
    assert app_module._file_stem("", "FY2025-26") == "account_FY2025-26"
    assert len(app_module._file_stem("A" * 500, "FY2025-26")) == 80


def test_run_deleted_mid_download_is_a_404(client, monkeypatch):
    d = _demo(client).get_json()
    real_expired = app_module._expired

    def vanish(path, now):          # the sweeper wins the race right after the checks
        expired = real_expired(path, now)
        app_module.shutil.rmtree(path)
        return expired
    monkeypatch.setattr(app_module, "_expired", vanish)
    assert client.get(d["downloads"]["pdf"]).status_code == 404


def test_file_gone_when_opened_is_a_404(client, monkeypatch):
    d = _demo(client).get_json()

    def gone(*a, **k):
        raise FileNotFoundError("deleted between listdir and open")
    monkeypatch.setattr(app_module, "send_file", gone)
    assert client.get(d["downloads"]["csv"]).status_code == 404


def test_full_results_dir_gives_503_and_leaves_nothing(client, monkeypatch):
    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(app_module, "_store_results", full)
    r = _demo(client)
    assert r.status_code == 503 and "busy" in r.get_json()["error"]


# ---------------------------------------------------------------- uploads


def test_upload_size_limit_returns_json_413(client, monkeypatch):
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 2000)
    r = client.post("/api/analyze", data={"files": (io.BytesIO(fy26.encode()), "a.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 413
    assert "the limit is 10 MB per request" in r.get_json()["error"]


def test_default_upload_limit_is_10_mb():
    assert app_module.app.config["MAX_CONTENT_LENGTH"] == 10 * 1024 * 1024


def test_uploads_are_never_spooled_to_disk(client, monkeypatch):
    # Werkzeug's default spools any upload over 500 KB to a temp file
    import werkzeug.formparser as fp

    def no_disk(*a, **k):
        raise AssertionError("upload spooled to a temporary file")
    monkeypatch.setattr(fp, "SpooledTemporaryFile", no_disk)
    monkeypatch.setattr(fp, "TemporaryFile", no_disk, raising=False)
    big = b"Statement,Header,Field Name,Field Value\n" + b"x,y\n" * 200_000   # ~800 KB
    r = client.post("/api/analyze", data={"files": (io.BytesIO(big), "big.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 422        # parsed (and refused) in memory, not crashed


def test_unexpected_failure_logs_no_statement_content(client, monkeypatch, caplog):
    def boom(report):
        raise ValueError("could not convert 'AAPL 123.45 U1234567'")
    monkeypatch.setattr(app_module, "build_pdf", boom)
    with caplog.at_level(logging.ERROR, logger="ibkr_tax_report"):
        r = _demo(client)
    assert r.status_code == 500
    assert "ValueError" in r.get_json()["error"]
    assert "AAPL" not in r.get_json()["error"]
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    assert "ValueError" in logged and "test_hosting.py" in logged
    assert "AAPL" not in logged and "U1234567" not in logged


# ---------------------------------------------------------------- rate limit


def test_rate_limit_returns_429_per_ip(client, monkeypatch):
    monkeypatch.setattr(app_module.SETTINGS, "rate_limits", [(2, 60)])
    assert _demo(client).status_code == 200
    assert _demo(client).status_code == 200
    r = _demo(client)
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert "Too many reports" in r.get_json()["error"]
    # uploads share the budget; other addresses are unaffected
    up = client.post("/api/analyze", data={"files": (io.BytesIO(fy26.encode()), "a.csv")},
                     content_type="multipart/form-data",
                     environ_base={"REMOTE_ADDR": "203.0.113.5"})
    assert up.status_code == 429
    assert _demo(client, ip="198.51.100.7").status_code == 200
    # downloads and deletes are not limited
    assert client.get("/", environ_base={"REMOTE_ADDR": "203.0.113.5"}).status_code == 200


def test_rate_limit_keys_on_forwarded_ip_behind_one_proxy(client, monkeypatch):
    monkeypatch.setattr(app_module.SETTINGS, "rate_limits", [(1, 60)])
    monkeypatch.setattr(app_module.app, "wsgi_app", ProxyFix(app_module.app.wsgi_app, x_for=1))
    caddy = "172.18.0.2"           # every request reaches the app from the proxy's address

    def via_proxy(xff):
        return client.post("/api/demo", environ_base={"REMOTE_ADDR": caddy},
                           headers={"X-Forwarded-For": xff})
    assert via_proxy("203.0.113.5").status_code == 200
    assert via_proxy("203.0.113.5").status_code == 429
    assert via_proxy("198.51.100.7").status_code == 200   # not one shared bucket for the proxy
    # a client-supplied X-Forwarded-For is to the left of the one the proxy adds:
    # only the right-most hop counts, so it cannot dodge the limit
    assert via_proxy("10.9.9.9, 203.0.113.5").status_code == 429


def test_rate_limiter_windows():
    rl = RateLimiter()
    lim = [(2, 60), (3, 3600)]
    assert rl.hit("a", lim, now=0) == 0
    assert rl.hit("a", lim, now=10) == 0
    assert rl.hit("a", lim, now=20) == 40        # first hit leaves the minute window at 60
    assert rl.hit("a", lim, now=59) == 1
    assert rl.hit("a", lim, now=60) == 0         # refused hits were not counted
    assert rl.hit("a", lim, now=200) == 3400     # hourly budget spent; frees at t=3600
    assert rl.hit("b", lim, now=200) == 0
    assert rl.hit("a", [], now=200) == 0         # no limits configured


def test_rate_limiter_forgets_addresses_after_the_longest_window():
    rl = RateLimiter()
    lim = [(2, 60), (3, 3600)]
    rl.hit("203.0.113.5", lim, now=0)
    rl.hit("198.51.100.7", lim, now=100)
    rl.prune(lim, now=3599)
    assert len(rl) == 2
    rl.prune(lim, now=3600)
    assert len(rl) == 1                          # gone exactly an hour after its last request
    rl.prune(lim, now=3700)
    assert len(rl) == 0


def test_sweeper_prunes_addresses_without_further_requests(monkeypatch):
    # the page promises the IP is kept at most an hour; that must not depend
    # on someone else making a request later
    monkeypatch.setattr(app_module.SETTINGS, "rate_limits", [(10, 60)])
    app_module.limiter.hit("203.0.113.5", [(10, 60)], now=time.monotonic() - 61)
    assert len(app_module.limiter) == 1

    class Once:
        n = 0

        def wait(self, delay):
            self.n += 1
            return self.n > 1
    app_module._sweep_loop(Once(), interval=30)
    assert len(app_module.limiter) == 0
