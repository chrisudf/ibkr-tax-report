"""Local web app: drag & drop IBKR statement CSVs, get an AU CGT report.

Privacy model: everything runs on this machine. Uploads are processed in
memory; generated artifacts are held in a per-run temp directory for download
and swept after RESULT_TTL. Nothing is sent anywhere.
"""
from __future__ import annotations

import os
import secrets
import shutil
import tempfile
import threading
import time

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from engine import (Options, ParseError, EngineError, FxError, RbaRates, TOOL_VERSION,
                    build_pdf, build_workpaper_csv, build_zip, compute_tax_report,
                    fy_of, merge_statements, parse_statement)

MAX_FILES = 12
RESULT_TTL = 60 * 60  # seconds
PREVIEW_ROWS = 200

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

_RESULTS_ROOT = os.path.join(tempfile.gettempdir(), "ibkr-tax-report-results")
os.makedirs(_RESULTS_ROOT, exist_ok=True)
_fx_lock = threading.Lock()
_fx: RbaRates | None = None


def get_fx() -> RbaRates:
    global _fx
    with _fx_lock:
        if _fx is None:
            _fx = RbaRates()
        return _fx


def _sweep_old_results() -> None:
    now = time.time()
    try:
        for name in os.listdir(_RESULTS_ROOT):
            p = os.path.join(_RESULTS_ROOT, name)
            if now - os.path.getmtime(p) > RESULT_TTL:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/analyze")
def analyze():
    _sweep_old_results()
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="no files uploaded"), 400
    if len(files) > MAX_FILES:
        return jsonify(error=f"too many files (max {MAX_FILES})"), 400

    stmts = []
    try:
        for f in files:
            # browsers may send a Windows path; strip both separators on any OS
            name = os.path.basename((f.filename or "statement.csv").replace("\\", "/"))
            stmts.append(parse_statement(f.read(), name))
        merged = merge_statements(stmts)
    except ParseError as e:
        return jsonify(error=str(e)), 422
    except Exception as e:
        return jsonify(error=f"could not read statements: {e}"), 422

    fy_default = fy_of(merged.period_end) if merged.period_end else None
    try:
        fy = int(request.form.get("fy") or fy_default or 0)
    except ValueError:
        return jsonify(error="invalid financial year"), 400
    if not fy:
        return jsonify(error="financial year could not be determined — pass fy"), 400
    entity = (request.form.get("entity") or "individual").lower()
    try:
        carried = float(request.form.get("carried_losses") or 0.0)
    except ValueError:
        return jsonify(error="invalid carried losses amount"), 400
    if carried < 0:
        return jsonify(error="carried losses must be a positive number"), 400

    try:
        report = compute_tax_report(merged, Options(
            fy_end_year=fy, entity=entity, carried_losses=carried), get_fx())
        workpaper = build_workpaper_csv(report)
        pdf = build_pdf(report)
        bundle = build_zip(report, workpaper, pdf)
    except (EngineError, FxError) as e:
        return jsonify(error=str(e)), 422

    token = secrets.token_urlsafe(16)
    run_dir = os.path.join(_RESULTS_ROOT, token)
    os.makedirs(run_dir)
    stem = f"{report['meta']['account'] or 'account'}_{report['meta']['fy']}"
    with open(os.path.join(run_dir, f"{stem}_workpaper.csv"), "w",
              encoding="utf-8-sig", newline="") as fh:
        fh.write(workpaper)
    with open(os.path.join(run_dir, f"{stem}_report.pdf"), "wb") as fh:
        fh.write(pdf)
    with open(os.path.join(run_dir, f"{stem}_bundle.zip"), "wb") as fh:
        fh.write(bundle)

    def preview(rows):
        return dict(rows=rows[:PREVIEW_ROWS], total=len(rows))

    return jsonify(
        token=token,
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
        ),
        downloads=dict(
            csv=f"/api/download/{token}/csv",
            pdf=f"/api/download/{token}/pdf",
            zip=f"/api/download/{token}/zip",
        ),
        fy_detected=fy_default,
        tool_version=TOOL_VERSION,
    )


_KIND_EXT = {"csv": "_workpaper.csv", "pdf": "_report.pdf", "zip": "_bundle.zip"}
_KIND_MIME = {"csv": "text/csv", "pdf": "application/pdf", "zip": "application/zip"}


@app.get("/api/download/<token>/<kind>")
def download(token: str, kind: str):
    if kind not in _KIND_EXT or not all(c.isalnum() or c in "-_" for c in token):
        abort(404)
    run_dir = os.path.join(_RESULTS_ROOT, token)
    if not os.path.isdir(run_dir):
        abort(404)
    for name in os.listdir(run_dir):
        if name.endswith(_KIND_EXT[kind]):
            return send_file(os.path.join(run_dir, name), mimetype=_KIND_MIME[kind],
                             as_attachment=True, download_name=name)
    abort(404)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5173, debug=False)
