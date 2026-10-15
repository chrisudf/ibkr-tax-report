"""Web app: drag & drop IBKR statement CSVs, get an AU CGT report.

Two modes, chosen by the IBKR_TAX_HOSTED environment variable:

- Local (default): `python app.py` serves http://127.0.0.1:5173 to this
  machine only. Nothing is sent anywhere.
- Hosted (IBKR_TAX_HOSTED=1): run by gunicorn behind a TLS reverse proxy, in
  the container described in deploy/. Files arrive over HTTPS from the
  visitor's browser.

Privacy model, both modes:

- Uploaded CSVs are parsed in memory and never written to disk. Werkzeug
  would spool any upload over 500 KB to a temp file; _InMemoryRequest keeps
  every file part in a BytesIO instead.
- The generated report files (workpaper CSV, PDF, ZIP) are written to
  RESULTS_DIR so the browser can download them — a RAM-backed tmpfs in the
  hosted container — and a background sweeper deletes each run RESULT_TTL
  after it was made. DELETE /api/result/<token> deletes them at once.
- File contents are never logged. A report that fails unexpectedly logs the
  exception type and source line only, not its message. There is no access
  log, no analytics, no cookies. The only per-visitor state is the client IP
  and request times, held in memory for the rate limiter's window.

Settings (all optional; deploy/docker-compose.yml sets the hosted ones):

  IBKR_TAX_HOSTED              1 = hosted page text, HSTS, default rate limit
  IBKR_TAX_RESULT_TTL_MINUTES  how long report files are kept (default 15)
  IBKR_TAX_RESULTS_DIR         where report files are kept (default: system temp)
  IBKR_TAX_MAX_UPLOAD_MB       request size limit (default 10)
  IBKR_TAX_RATE_LIMIT          "count/seconds,..." per client IP for report runs;
                               default "10/60,60/3600" when hosted, off locally
  IBKR_TAX_PROXY_HOPS          reverse proxies in front (1 behind Caddy); the
                               client IP is taken from X-Forwarded-For
  IBKR_TAX_RBA_DIR             writable directory for refreshed RBA tables
  IBKR_TAX_RBA_REFRESH_HOURS   refresh interval (needs IBKR_TAX_RBA_DIR; 0 = off)
  IBKR_TAX_BUILD               build id shown next to the version (git commit)
"""
from __future__ import annotations

import io
import logging
import math
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from functools import wraps

from flask import (Flask, Request, abort, jsonify, render_template, request, send_file)
from werkzeug.middleware.proxy_fix import ProxyFix

import rba_refresh
from engine import (Options, ParseError, EngineError, FxError, RbaRates, TOOL_VERSION,
                    build_pdf, build_workpaper_csv, build_zip, compute_tax_report,
                    detect_fy, merge_statements, parse_statement)
from engine.fx import DATA_FILE as BUNDLED_RBA

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_URL = "https://github.com/chrisudf/ibkr-tax-report"
ISSUES_URL = REPO_URL + "/issues"
DEMO_DIR = os.path.join(HERE, "demo")
DEMO_FILES = ("demo_fy2025.csv", "demo_fy2026.csv")
MAX_FILES = 12
PREVIEW_ROWS = 200

log = logging.getLogger("ibkr_tax_report")


def _parse_limits(spec: str) -> list[tuple[int, int]]:
    """"10/60,60/3600" -> [(10, 60), (60, 3600)]: at most 10 per 60 s and 60 per hour."""
    limits = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        count, _, window = part.partition("/")
        limits.append((int(count), int(window)))
    if any(n < 1 or w < 1 for n, w in limits):
        raise ValueError(f"bad IBKR_TAX_RATE_LIMIT {spec!r}")
    return limits


@dataclass
class Settings:
    hosted: bool = False
    result_ttl: int = 15 * 60            # seconds
    results_dir: str = os.path.join(tempfile.gettempdir(), "ibkr-tax-report-results")
    max_upload_mb: int = 10
    rate_limits: list[tuple[int, int]] = field(default_factory=list)
    proxy_hops: int = 0
    rba_dir: str = ""
    rba_refresh_hours: float = 0.0
    build: str = ""

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        s = cls()
        s.hosted = env.get("IBKR_TAX_HOSTED", "").strip().lower() in ("1", "true", "yes")
        s.result_ttl = int(float(env.get("IBKR_TAX_RESULT_TTL_MINUTES") or 15) * 60)
        s.results_dir = env.get("IBKR_TAX_RESULTS_DIR") or s.results_dir
        s.max_upload_mb = int(env.get("IBKR_TAX_MAX_UPLOAD_MB") or 10)
        spec = env.get("IBKR_TAX_RATE_LIMIT")
        if spec is None and s.hosted:
            spec = "10/60,60/3600"
        s.rate_limits = _parse_limits(spec or "")
        s.proxy_hops = int(env.get("IBKR_TAX_PROXY_HOPS") or 0)
        s.rba_dir = env.get("IBKR_TAX_RBA_DIR") or ""
        s.rba_refresh_hours = float(env.get("IBKR_TAX_RBA_REFRESH_HOURS") or 0)
        s.build = (env.get("IBKR_TAX_BUILD") or "").strip()
        return s


SETTINGS = Settings.from_env()


class _InMemoryRequest(Request):
    """Keep uploaded files in memory. The default stream factory spools any
    upload over 500 KB to a temp file on disk; the request size limit
    (MAX_CONTENT_LENGTH) bounds what this can hold."""

    def _get_file_stream(self, total_content_length, content_type, filename=None,
                         content_length=None):
        return io.BytesIO()


app = Flask(__name__, static_folder="static", static_url_path="/static")
app.request_class = _InMemoryRequest
app.config["MAX_CONTENT_LENGTH"] = SETTINGS.max_upload_mb * 1024 * 1024
if SETTINGS.proxy_hops:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=SETTINGS.proxy_hops,
                            x_proto=SETTINGS.proxy_hops)

# ---------------------------------------------------------------- RBA rates

_fx_lock = threading.Lock()
_fx: RbaRates | None = None
_rba_status: dict = {"source": "bundled table", "last_check": None, "last_outcome": None}


def _rba_refreshed_path() -> str | None:
    return os.path.join(SETTINGS.rba_dir, "rba_f11.csv") if SETTINGS.rba_dir else None


def get_fx() -> RbaRates:
    global _fx
    with _fx_lock:
        if _fx is None:
            _fx, _rba_status["source"] = rba_refresh.load_best(BUNDLED_RBA,
                                                               _rba_refreshed_path())
        return _fx


def refresh_rba(fetch_fn=rba_refresh.fetch) -> rba_refresh.Outcome:
    """One validated refresh. On success the new table is swapped in for
    every report started afterwards; runs in progress keep the old one."""
    global _fx
    dest = _rba_refreshed_path()
    if not dest:
        return rba_refresh.Outcome("failed", "IBKR_TAX_RBA_DIR is not set")
    os.makedirs(SETTINGS.rba_dir, exist_ok=True)
    outcome = rba_refresh.refresh(get_fx(), dest, fetch_fn)
    if outcome.status == "updated":
        with _fx_lock:
            _fx = outcome.rates
            _rba_status["source"] = f"refreshed copy {dest}"
    _rba_status["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _rba_status["last_outcome"] = f"{outcome.status}: {outcome.detail}"
    level = logging.WARNING if outcome.status in ("rejected", "failed") else logging.INFO
    log.log(level, "RBA refresh %s: %s", outcome.status, outcome.detail)
    return outcome


# ---------------------------------------------------------------- results


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_KIND_EXT = {"csv": "_workpaper.csv", "pdf": "_report.pdf", "zip": "_bundle.zip"}
_KIND_MIME = {"csv": "text/csv", "pdf": "application/pdf", "zip": "application/zip"}


def _results_root() -> str:
    os.makedirs(SETTINGS.results_dir, exist_ok=True)
    return SETTINGS.results_dir


def _run_dir(token: str) -> str | None:
    """The result directory for a token, if the token is well formed."""
    if not _TOKEN_RE.match(token):
        return None
    return os.path.join(_results_root(), token)


def _expired(path: str, now: float) -> bool:
    try:
        return now - os.path.getmtime(path) >= SETTINGS.result_ttl
    except OSError:
        return True


def sweep_results(now: float | None = None) -> int:
    """Delete every run older than the TTL. Returns how many were removed."""
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(_results_root())
    except OSError:
        return 0
    for name in names:
        p = os.path.join(SETTINGS.results_dir, name)
        if _expired(p, now):
            shutil.rmtree(p, ignore_errors=True)
            removed += not os.path.exists(p)
    return removed


def _file_stem(account: str, fy: str) -> str:
    """Report file names come from the statement's Account field, which is
    whatever the uploaded CSV says. Reduce them to one plain file-name
    component so a crafted value ("../x", "/etc/x") cannot place files
    outside the run directory, where deletion and expiry would miss them."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{account or 'account'}_{fy}").strip("_")
    return stem[:80] or "report"


def _store_results(stem: str, workpaper: str, pdf: bytes, bundle: bytes) -> str:
    token = secrets.token_urlsafe(16)
    run_dir = os.path.join(_results_root(), token)
    os.makedirs(run_dir)
    try:
        with open(os.path.join(run_dir, f"{stem}_workpaper.csv"), "w",
                  encoding="utf-8-sig", newline="") as fh:
            fh.write(workpaper)
        with open(os.path.join(run_dir, f"{stem}_report.pdf"), "wb") as fh:
            fh.write(pdf)
        with open(os.path.join(run_dir, f"{stem}_bundle.zip"), "wb") as fh:
            fh.write(bundle)
    except OSError:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return token


# ---------------------------------------------------------------- background


_bg_lock = threading.Lock()
_bg_stop = threading.Event()
_bg_threads: list[threading.Thread] = []


def _sweep_loop(stop: threading.Event, interval: float) -> None:
    while not stop.wait(interval):
        try:
            sweep_results()
            limiter.prune(SETTINGS.rate_limits)
        except Exception:  # keep the sweeper alive whatever happens
            log.exception("result sweep failed")


def _rba_loop(stop: threading.Event, interval: float) -> None:
    delay = 60.0   # first check shortly after start: the tmpfs copy is empty after a restart
    while not stop.wait(delay):
        try:
            outcome = refresh_rba()
        except Exception:
            log.exception("RBA refresh crashed")
            outcome = None
        # a network failure is worth retrying sooner than a week; a rejected
        # file will most likely still be rejected tomorrow
        delay = interval if outcome and outcome.status != "failed" else min(interval, 6 * 3600)


def start_background() -> None:
    """Start the result sweeper and, when configured, the RBA refresher.
    Idempotent; call once per serving process (gunicorn: post_worker_init)."""
    with _bg_lock:
        if _bg_threads:
            return
        if not logging.getLogger().handlers and not log.handlers:
            logging.basicConfig(level=logging.INFO,
                                format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        log.setLevel(logging.INFO)
        get_fx()
        log.info("RBA rates: %s, USD %s to %s", _rba_status["source"], *get_fx().coverage())
        sweep_interval = min(30.0, max(1.0, SETTINGS.result_ttl / 30))
        jobs = [("result-sweeper", _sweep_loop, sweep_interval)]
        if SETTINGS.rba_dir and SETTINGS.rba_refresh_hours > 0:
            jobs.append(("rba-refresh", _rba_loop, SETTINGS.rba_refresh_hours * 3600))
        for name, target, interval in jobs:
            t = threading.Thread(target=target, args=(_bg_stop, interval), name=name,
                                 daemon=True)
            t.start()
            _bg_threads.append(t)
        log.info("background: %s; results kept %d min in %s",
                 ", ".join(t.name for t in _bg_threads), SETTINGS.result_ttl // 60,
                 SETTINGS.results_dir)


def stop_background(timeout: float = 5.0) -> None:
    _bg_stop.set()
    for t in _bg_threads:
        t.join(timeout)
    _bg_threads.clear()
    _bg_stop.clear()


# ---------------------------------------------------------------- rate limit


class RateLimiter:
    """Sliding-window limits per key, in memory. Refused requests are not
    counted, so retrying while blocked does not extend the block."""

    def __init__(self):
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)

    def prune(self, limits: list[tuple[int, int]], now: float | None = None) -> None:
        """Forget requests older than the longest window, and every address
        left with none. The result sweeper calls this, so addresses go even
        when no further requests arrive."""
        now = time.monotonic() if now is None else now
        horizon = now - max((w for _, w in limits), default=0)
        with self._lock:
            for key in list(self._hits):
                q = self._hits[key]
                while q and q[0] <= horizon:
                    q.popleft()
                if not q:
                    del self._hits[key]

    def hit(self, key: str, limits: list[tuple[int, int]], now: float | None = None) -> int:
        """Record a request; 0 if allowed, else seconds until one would be."""
        if not limits:
            return 0
        now = time.monotonic() if now is None else now
        longest = max(w for _, w in limits)
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - longest:
                q.popleft()
            wait = 0.0
            for n, w in limits:
                recent = [t for t in q if t > now - w]
                if len(recent) >= n:
                    # allowed again once the oldest hit that keeps us at the limit leaves the window
                    wait = max(wait, recent[len(recent) - n] + w - now)
            if wait > 0:
                return max(1, math.ceil(wait))
            q.append(now)
            return 0


limiter = RateLimiter()


def rate_limited(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        retry = limiter.hit(request.remote_addr or "?", SETTINGS.rate_limits)
        if retry:
            when = f"{retry} seconds" if retry < 120 else f"about {math.ceil(retry / 60)} minutes"
            resp = jsonify(error="Too many reports from your address in a short time — "
                                 f"try again in {when}.")
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry)
            return resp
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------- pages


@app.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'")
    if SETTINGS.hosted:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.path.startswith("/api/") or request.path == "/":
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _version() -> str:
    return f"v{TOOL_VERSION}" + (f" ({SETTINGS.build})" if SETTINGS.build else "")


@app.get("/")
def index():
    first, last = get_fx().coverage("USD")
    return render_template(
        "index.html", hosted=SETTINGS.hosted, ttl_minutes=SETTINGS.result_ttl // 60,
        max_upload_mb=SETTINGS.max_upload_mb, version=_version(),
        rba_first=f"{first.day} {first:%b %Y}", rba_last=f"{last.day} {last:%b %Y}",
        repo_url=REPO_URL, issues_url=ISSUES_URL)


@app.get("/healthz")
def healthz():
    first, last = get_fx().coverage("USD")
    return jsonify(ok=True, version=_version(),
                   rba=dict(first=str(first), last=str(last), **_rba_status))


@app.errorhandler(413)
def _too_large(_e):
    return jsonify(error=f"Upload too large: the limit is {SETTINGS.max_upload_mb} MB per "
                         "request. A year's IBKR Activity Statement CSV is usually a few "
                         "hundred KB, even for an active options trader — check that these "
                         "are the CSV statement exports, not PDFs or Flex query files."), 413


# ---------------------------------------------------------------- reports


def _report_response(uploads: list[tuple[str, bytes]], form, demo: bool = False):
    stmts = []
    for name, content in uploads:
        try:
            stmts.append(parse_statement(content, name))
        except ParseError as e:
            return jsonify(error=str(e)), 422
        except Exception as e:  # the parser's own errors are ParseError; this is a backstop
            return jsonify(error=f"{name} could not be read ({type(e).__name__}: {e}). Upload "
                                 "the CSV export of an IBKR Activity Statement or Realized "
                                 "Summary, unmodified."), 422
    try:
        merged = merge_statements(stmts)
    except ParseError as e:
        return jsonify(error=str(e)), 422

    fy_default = detect_fy(merged)
    try:
        fy = int(form.get("fy") or fy_default or 0)
    except ValueError:
        return jsonify(error="invalid financial year"), 400
    if not fy:
        return jsonify(error="financial year could not be determined — pass fy"), 400
    entity = (form.get("entity") or "individual").lower()
    try:
        carried = float(form.get("carried_losses") or 0.0)
    except ValueError:
        return jsonify(error="invalid carried losses amount"), 400
    if not math.isfinite(carried) or carried < 0:
        return jsonify(error="carried losses must be a positive number"), 400

    try:
        report = compute_tax_report(merged, Options(
            fy_end_year=fy, entity=entity, carried_losses=carried), get_fx())
        workpaper = build_workpaper_csv(report)
        pdf = build_pdf(report)
        bundle = build_zip(report, workpaper, pdf)
    except (EngineError, FxError) as e:
        return jsonify(error=str(e)), 422
    except Exception as e:
        # Log where it broke, never the message: it can quote statement content.
        frame = traceback.extract_tb(e.__traceback__)[-1]
        log.error("report failed: %s at %s:%d", type(e).__name__,
                  os.path.relpath(frame.filename, HERE), frame.lineno)
        return jsonify(error=f"The report could not be built ({type(e).__name__}). This is a "
                             f"bug — please report it at {ISSUES_URL}, describing the "
                             "statement but without attaching it."), 500

    stem = _file_stem(report["meta"]["account"], report["meta"]["fy"])
    try:
        token = _store_results(stem, workpaper, pdf, bundle)
    except OSError:
        log.error("could not store results (disk full?)")
        return jsonify(error="The server is busy — try again in a few minutes."), 503

    def preview(rows):
        return dict(rows=rows[:PREVIEW_ROWS], total=len(rows))

    return jsonify(
        token=token,
        demo=demo,
        meta=report["meta"],
        summary=report["summary"],
        warnings=report["warnings"],
        amendment_flags=report["amendment_flags"],
        cross_year_notes=report["cross_year_notes"],
        reconciliation=report["reconciliation"],
        tables=dict(
            closed_lots=preview(report["closed_lots"]),
            d2_open=preview(report["d2_open"]),
            transfers=preview(report["transfers"]),
            carry_forward=preview(report["carry_forward"]),
            unmatched=preview(report["unmatched"]),
            fx_realisations=preview(report["fx_realisations"]),
        ),
        forex_notes=report["forex_notes"],
        downloads=dict(
            csv=f"/api/download/{token}/csv",
            pdf=f"/api/download/{token}/pdf",
            zip=f"/api/download/{token}/zip",
        ),
        delete_url=f"/api/result/{token}",
        expires_in=SETTINGS.result_ttl,
        fy_detected=fy_default,
        tool_version=TOOL_VERSION,
    )


@app.post("/api/analyze")
@rate_limited
def analyze():
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="no files uploaded"), 400
    if len(files) > MAX_FILES:
        return jsonify(error=f"too many files (max {MAX_FILES})"), 400
    uploads = []
    for f in files:
        # browsers may send a Windows path; strip both separators on any OS
        name = os.path.basename((f.filename or "statement.csv").replace("\\", "/"))
        uploads.append((name, f.read()))
    return _report_response(uploads, request.form)


@app.post("/api/demo")
@rate_limited
def demo():
    """Run the bundled fictional statements (account U9999999) — nothing uploaded."""
    uploads = []
    for name in DEMO_FILES:
        with open(os.path.join(DEMO_DIR, name), "rb") as fh:
            uploads.append((name, fh.read()))
    return _report_response(uploads, request.form, demo=True)


@app.get("/api/download/<token>/<kind>")
def download(token: str, kind: str):
    run_dir = _run_dir(token)
    if kind not in _KIND_EXT or not run_dir or not os.path.isdir(run_dir):
        abort(404)
    if _expired(run_dir, time.time()):   # don't wait for the sweeper
        shutil.rmtree(run_dir, ignore_errors=True)
        abort(404)
    # The sweeper or a DELETE can remove the run between the checks above and
    # opening the file: that is a 404, not a 500 with the file name in the log.
    try:
        for name in os.listdir(run_dir):
            if name.endswith(_KIND_EXT[kind]):
                return send_file(os.path.join(run_dir, name), mimetype=_KIND_MIME[kind],
                                 as_attachment=True, download_name=name, max_age=0)
    except OSError:
        pass
    abort(404)


@app.delete("/api/result/<token>")
def delete_result(token: str):
    run_dir = _run_dir(token)
    if not run_dir or not os.path.isdir(run_dir):
        return jsonify(error="no such result — it may already have been deleted or expired"), 404
    shutil.rmtree(run_dir, ignore_errors=True)
    if os.path.exists(run_dir):
        return jsonify(error="could not delete the result files"), 500
    return jsonify(deleted=True)


if __name__ == "__main__":
    start_background()
    app.run(host="127.0.0.1", port=5173, debug=False)
