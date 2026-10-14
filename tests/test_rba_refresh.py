"""Validated RBA F11.1 refresh: accept only longer, consistent tables."""
import os
import urllib.error
from datetime import date, timedelta

import pytest

import app as app_module
import rba_refresh
from engine.fx import DATA_FILE, RbaRates
from rba_refresh import Rejected


def f11(days, start=date(2023, 1, 3), usd=0.6800, eur=0.6400, overrides=None):
    """A minimal F11.1-shaped file: `days` business days from `start`."""
    overrides = overrides or {}
    lines = ["﻿F11.1  EXCHANGE RATES",
             "Title,A$1=USD,Trade-weighted Index May 1970 = 100,A$1=EUR",
             "Units,USD,Index,EUR", "", "Series ID,FXRUSD,FXRTWI,FXREUR"]
    d, n = start, 0
    while n < days:
        if d.weekday() < 5:
            u, e = overrides.get(d, (round(usd + n / 10000, 4), eur))
            lines.append(f"{d:%d-%b-%Y},{u},60.0,{e}")
            n += 1
        d += timedelta(days=1)
    return ("\r\n".join(lines) + "\r\n" * 5).encode()   # the RBA pads with blank lines


def rates(tmp_path, data, name="t.csv"):
    p = tmp_path / name
    p.write_bytes(data)
    return RbaRates([str(p)])


# ---------------------------------------------------------------- compare


def test_longer_consistent_table_is_accepted(tmp_path):
    old, new = rates(tmp_path, f11(10), "a"), rates(tmp_path, f11(15), "b")
    assert rba_refresh.compare(old, new) == "updated"


def test_same_table_is_unchanged(tmp_path):
    old, new = rates(tmp_path, f11(10), "a"), rates(tmp_path, f11(10), "b")
    assert rba_refresh.compare(old, new) == "unchanged"


def test_shorter_table_is_rejected(tmp_path):
    old, new = rates(tmp_path, f11(10), "a"), rates(tmp_path, f11(8), "b")
    with pytest.raises(Rejected, match="earlier than the table in use"):
        rba_refresh.compare(old, new)


def test_table_starting_later_is_rejected(tmp_path):
    # the RBA moving its rolling window forward must not drop the 2023 rates
    old = rates(tmp_path, f11(10), "a")
    new = rates(tmp_path, f11(20, start=date(2023, 1, 5)), "b")
    with pytest.raises(Rejected, match="rates before then would be lost"):
        rba_refresh.compare(old, new)


def test_changed_overlapping_rate_is_rejected(tmp_path):
    old = rates(tmp_path, f11(10), "a")
    new = rates(tmp_path, f11(15, overrides={date(2023, 1, 9): (0.7, 0.64)}), "b")
    with pytest.raises(Rejected, match="USD on 2023-01-09 is 0.6804 .* 0.7 in the download"):
        rba_refresh.compare(old, new)


def test_changed_other_currency_is_rejected(tmp_path):
    old = rates(tmp_path, f11(10), "a")
    new = rates(tmp_path, f11(15, overrides={date(2023, 1, 4): (0.6801, 0.65)}), "b")
    with pytest.raises(Rejected, match="EUR on 2023-01-04"):
        rba_refresh.compare(old, new)


def test_missing_overlapping_row_is_rejected(tmp_path):
    old = rates(tmp_path, f11(10), "a")
    lines = f11(15).decode().split("\r\n")
    data = "\r\n".join(l for l in lines if not l.startswith("06-Jan-2023")).encode()
    new = rates(tmp_path, data, "b")
    with pytest.raises(Rejected, match="EUR on 2023-01-06 is 0.64 in the table in use, None in the download"):
        rba_refresh.compare(old, new)


def test_non_positive_new_rate_is_rejected(tmp_path):
    old = rates(tmp_path, f11(10), "a")
    new = rates(tmp_path, f11(12, overrides={date(2023, 1, 18): (0, 0.64)}), "b")
    with pytest.raises(Rejected, match="non-positive"):
        rba_refresh.compare(old, new)


@pytest.mark.parametrize("data", [
    b"<html><body>Access Denied</body></html>",
    b"Title,A$1=USD\r\n03-Jan-2023,abc\r\n",
    b"03-Jan-2023,0.68\r\n",
    b"",
])
def test_unparseable_download_is_rejected(tmp_path, data):
    p = tmp_path / "bad.csv"
    p.write_bytes(data)
    with pytest.raises(Rejected):
        rba_refresh.compare(RbaRates(), rba_refresh.load_candidate(str(p)))


def test_bundled_table_against_itself_and_one_more_day(tmp_path):
    bundled = RbaRates([DATA_FILE])
    assert rba_refresh.compare(bundled, RbaRates([DATA_FILE])) == "unchanged"
    with open(DATA_FILE, "rb") as fh:
        raw = fh.read().rstrip(b"\r\n")
    last = bundled.coverage("USD")[1]
    nxt = last + timedelta(days=1)
    row = raw.split(b"\n")[-1].strip(b"\r").split(b",")
    row[0] = f"{nxt:%d-%b-%Y}".encode()
    longer = rates(tmp_path, raw + b"\r\n" + b",".join(row) + b"\r\n", "longer.csv")
    assert rba_refresh.compare(bundled, longer) == "updated"
    assert longer.coverage("USD") == (bundled.coverage("USD")[0], nxt)


# ---------------------------------------------------------------- refresh


def test_refresh_writes_only_a_validated_table(tmp_path):
    current = rates(tmp_path, f11(10), "cur.csv")
    dest = tmp_path / "rba" / "rba_f11.csv"
    dest.parent.mkdir()

    out = rba_refresh.refresh(current, str(dest), lambda: f11(12))
    assert out.status == "updated" and out.rates.coverage("USD")[1] == date(2023, 1, 18)
    assert dest.read_bytes() == f11(12)

    tampered = f11(14, overrides={date(2023, 1, 3): (0.5, 0.64)})
    out = rba_refresh.refresh(out.rates, str(dest), lambda: tampered)
    assert out.status == "rejected" and "2023-01-03" in out.detail
    assert dest.read_bytes() == f11(12)                      # old table kept
    assert os.listdir(dest.parent) == ["rba_f11.csv"]        # no temp file left behind


def test_refresh_reports_network_failure(tmp_path):
    def offline():
        raise urllib.error.URLError("Temporary failure in name resolution")
    out = rba_refresh.refresh(RbaRates(), str(tmp_path / "x.csv"), offline)
    assert out.status == "failed" and "name resolution" in out.detail
    assert os.listdir(tmp_path) == []


def test_refresh_unchanged_does_not_write(tmp_path):
    current = rates(tmp_path, f11(10), "cur.csv")
    out = rba_refresh.refresh(current, str(tmp_path / "dest.csv"), lambda: f11(10))
    assert out.status == "unchanged"
    assert not (tmp_path / "dest.csv").exists()


def test_load_best_prefers_valid_newer_copy(tmp_path):
    bundled = tmp_path / "bundled.csv"
    bundled.write_bytes(f11(10))
    newer = tmp_path / "newer.csv"
    newer.write_bytes(f11(12))
    fx, src = rba_refresh.load_best(str(bundled), str(newer))
    assert fx.coverage("USD")[1] == date(2023, 1, 18) and "refreshed" in src
    newer.write_bytes(b"garbage")
    fx, src = rba_refresh.load_best(str(bundled), str(newer))
    assert fx.coverage("USD")[1] == date(2023, 1, 16) and "ignored" in src
    fx, src = rba_refresh.load_best(str(bundled), str(tmp_path / "missing.csv"))
    assert src == "bundled table"


# ---------------------------------------------------------------- in the app


@pytest.fixture
def app_with_small_table(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module.SETTINGS, "rba_dir", str(tmp_path / "rba"))
    monkeypatch.setattr(app_module, "_fx", rates(tmp_path, f11(10), "start.csv"))
    monkeypatch.setattr(app_module, "_rba_status", dict(app_module._rba_status))
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_app_hot_swaps_a_validated_table(app_with_small_table):
    client = app_with_small_table
    assert client.get("/healthz").get_json()["rba"]["last"] == "2023-01-16"
    out = app_module.refresh_rba(lambda: f11(12))
    assert out.status == "updated"
    h = client.get("/healthz").get_json()["rba"]
    assert h["last"] == "2023-01-18" and h["last_outcome"].startswith("updated")
    assert "18 Jan 2023" in client.get("/").get_data(as_text=True)
    assert os.path.exists(os.path.join(app_module.SETTINGS.rba_dir, "rba_f11.csv"))


def test_app_keeps_old_table_when_refresh_is_rejected(app_with_small_table, caplog):
    client = app_with_small_table
    out = app_module.refresh_rba(lambda: f11(12, overrides={date(2023, 1, 4): (1.0, 0.64)}))
    assert out.status == "rejected"
    h = client.get("/healthz").get_json()["rba"]
    assert h["last"] == "2023-01-16" and h["last_outcome"].startswith("rejected")
    assert any("RBA refresh rejected" in r.getMessage() for r in caplog.records)


def test_rba_loop_retries_a_failed_download_sooner(app_with_small_table, monkeypatch):
    real_refresh = app_module.refresh_rba
    downloads = iter([None, f11(12), f11(12)])     # offline first, then a newer table

    def fetch():
        data = next(downloads)
        if data is None:
            raise urllib.error.URLError("timed out")
        return data
    monkeypatch.setattr(app_module, "refresh_rba", lambda: real_refresh(fetch))

    class Clock:     # stands in for the stop Event: records each delay, stops on the 4th wait
        delays = []

        def wait(self, delay):
            self.delays.append(delay)
            return len(self.delays) > 3
    week = 7 * 24 * 3600
    app_module._rba_loop(Clock(), interval=week)
    # first check soon after start; a failure retries in 6 h, not a week
    assert Clock.delays == [60.0, 6 * 3600, week, week]
    assert app_module.get_fx().coverage("USD")[1] == date(2023, 1, 18)
    assert app_module._rba_status["last_outcome"].startswith("unchanged")
