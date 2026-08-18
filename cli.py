"""Command-line runner: same engine as the web app, for scripted use.

    python cli.py statement1.csv [statement2.csv ...] \
        [--fy 2026] [--entity individual] [--carried-losses 1944] [-o outdir]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Warnings contain em dashes and other non-ASCII; the legacy Windows console
# (cp1252/cp936) would otherwise raise UnicodeEncodeError on print.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from engine import (Options, RbaRates, build_pdf, build_workpaper_csv, build_zip,
                    compute_tax_report, fy_of, merge_statements, parse_statement)


def main() -> int:
    ap = argparse.ArgumentParser(description="IBKR AU CGT report generator")
    ap.add_argument("files", nargs="+", help="IBKR Activity Statement / Realized Summary CSVs")
    ap.add_argument("--fy", type=int, default=None,
                    help="financial year END year (e.g. 2026 = FY2025-26); default: from statements")
    ap.add_argument("--entity", default="individual",
                    choices=["individual", "trust", "smsf", "company"])
    ap.add_argument("--carried-losses", type=float, default=0.0,
                    help="prior-year net capital losses to apply (AUD)")
    ap.add_argument("-o", "--outdir", default=".", help="output directory")
    args = ap.parse_args()

    stmts = []
    for p in args.files:
        with open(p, "rb") as fh:
            stmts.append(parse_statement(fh.read(), os.path.basename(p)))
    merged = merge_statements(stmts)
    fy = args.fy or (fy_of(merged.period_end) if merged.period_end else None)
    if not fy:
        print("could not determine financial year — pass --fy", file=sys.stderr)
        return 2

    report = compute_tax_report(merged, Options(
        fy_end_year=fy, entity=args.entity, carried_losses=args.carried_losses),
        RbaRates())
    workpaper = build_workpaper_csv(report)
    pdf = build_pdf(report)
    bundle = build_zip(report, workpaper, pdf)

    os.makedirs(args.outdir, exist_ok=True)
    stem = os.path.join(args.outdir, f"{report['meta']['account']}_{report['meta']['fy']}")
    with open(f"{stem}_workpaper.csv", "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(workpaper)
    with open(f"{stem}_report.pdf", "wb") as fh:
        fh.write(pdf)
    with open(f"{stem}_bundle.zip", "wb") as fh:
        fh.write(bundle)

    print(json.dumps(report["summary"], indent=1))
    for w in report["warnings"]:
        print("WARNING:", w, file=sys.stderr)
    print(f"wrote {stem}_workpaper.csv / _report.pdf / _bundle.zip", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
