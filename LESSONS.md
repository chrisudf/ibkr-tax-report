# Lessons

Problems met while building and running this tool, what caused them, and what
we do now. Newest first.

## 2026-10-16 — the rest of the tax-logic audit

- **IBKR's day is not the calendar day.** It files rows by their US report
  date and its statement day ends the next morning in Sydney, so a statement
  carries trades dated the day after it ends, and the next statement's opening
  positions already include them. Cutting files at Sydney midnight raised
  false "positions do not add up" warnings and, with an export lacking the
  Mark-to-Market summary, invented a parcel. Decide which side of a boundary a
  row is on by the file it came from, not by its date.
- **A placeholder date is not a date.** Positions held before the data get a
  stand-in opening time just before the data starts. One loop took it for a
  write date inside the year and counted a premium as this year's D2 gain. Any
  code that compares dates has to skip, or say it is skipping, rows whose date
  is unknown.
- **Do not look up what this year does not need.** A run for one year failed on
  a rate past the end of the RBA table for a trade months after 30 June, used
  only for a correction this year never reads. Stricter input rules (here, no
  extrapolated rates) turn such lookups into hard failures.
- **One figure, one meaning.** A row labelled FITO held the full withholding,
  including the part the warning below it said was not FITO. If a figure needs
  a qualification, give the qualified figure its own row.
- **Cite the provision that says it.** s 134-1(1) item 1 is the call; a put is
  item 2. s 112-155 is individuals; trusts are s 112-165. s 2G of the Acts
  Interpretation Act defines a month; leaving out the two end days is the
  ATO's approach. A reviewer checking each citation against the Act found all
  of these.
- **Review the fixes, not just the bugs.** An independent review of the fix
  branch found three bugs the fixes introduced or missed, each reproduced
  with a synthetic statement before it was fixed.

## 2026-10-15 — Div 775 in USD-base accounts

- **A broker's "FX P/L" is measured in the broker's base currency, not yours.**
  The tool reported IBKR's Forex P/L Details as Div 775. In a USD-base account
  that section is the gain or loss on the *AUD* cash measured in USD, which
  for an Australian resident is no forex gain at all. Meanwhile the real
  exposure, the USD cash, was not counted anywhere (found by a tax-logic
  audit). Before relying on a figure from a source, ask whose currency it is
  in and what it measures.
- **Build it from the primitives, then reconcile with the source's totals.**
  Div 775 is now worked out FIFO from the statement's own cash flows, and each
  statement's Cash Report checks that every flow was read. The real statements
  reconcile within US$1–2, the GST on fees that IBKR does not itemise.
- **A statement's totals include rows dated outside its own period.** The first
  reconciliation selected flows by the statement's dates after merging files,
  and was US$18–88 out on real data: IBKR lists, for example, June interest
  posted in early July in the June statement. Each Cash Report is now checked
  against the rows of its own file. Synthetic test data had not shown this;
  real data did within minutes.
- **Use the regulator's worked example as the golden test.** The ATO's FIFO
  example (deposits, withdrawals, overdraft, repayment) is reproduced to the
  cent in `tests/test_forex.py`. A property test checks that every AUD amount
  in or out is either realised or still in an open lot, whatever the order.
- **Check the section numbers.** s 775-145 turned out to be the FIFO rule, not
  the $250,000 election (s 775-230). Legislative references go in only after
  checking them against the ATO's own pages.

## 2026-10-15 — hosting at cgt.nomad403.cc (PRs #2, #3)

### Privacy claims have to be true of the code, not of the intent

- **"Uploads are never written to disk" was false by default.** Werkzeug's
  form parser spools any multipart upload over 500 KB to a temporary file.
  `app.py` now uses a request class whose `_get_file_stream` returns a
  `BytesIO`; the 10 MB request limit bounds it. A test patches the temp-file
  factories to fail and uploads 800 KB.
- **RAM is not "not disk" if it can be swapped.** The droplet has a swapfile,
  so container memory, and with it the tmpfs holding results, could be paged
  out. The compose file sets `memswap_limit` equal to `mem_limit`; the
  container's `memory.swap.max` reads 0.
- **"IP held for up to an hour" needed a timer.** The rate limiter only
  pruned old addresses when a new request arrived, so with no traffic the last
  visitors stayed in memory indefinitely (found by review). The result sweeper
  now prunes the limiter every 30 s.
- **Exception messages can quote the statement.** An unexpected failure logs
  only the exception type and source line, never `str(e)`. A download race used
  to produce a 500 whose traceback held the account-bearing file name; it is now
  a 404.
- **Check what the proxy logs too.** The Caddy site block has no `log`
  directive, and gunicorn runs without an access log (download URLs carry
  result tokens).

### Anything from the uploaded file is untrusted, including names

- Report file names were built from the CSV's `Account` field, so an Account
  of `../x` or an absolute path wrote files outside the run directory, where
  delete and expiry never looked (found by review). File names are now reduced
  to `[A-Za-z0-9_-]`. The same applies to any value that ends up in a path,
  header or log.

### Validation must reject what the arithmetic cannot survive

- The RBA refresh checked `rate > 0`, which `inf` passes; amounts are divided by
  the rate, so `inf` silently becomes A$0. Rows dated before the table in use
  were not checked at all. Every rate must now be finite and positive, and that
  check runs before the overlap comparison, so a bad row is rejected for the
  right reason.
- `tempfile.mkstemp()` sat outside the `try` that turned failures into an
  outcome, so a missing or full directory raised out of the refresh loop instead
  of showing up in `/healthz`. Code that promises "never raises" needs its first
  line inside the `try` too.

### Races are ordinary in a server with background cleanup

- The sweeper or a DELETE can remove a run between "does it exist?" and
  "open it". Downloads catch `OSError` around listing and `send_file()`.
- In the page, a DELETE that finished after a newer report had rendered
  disabled the new report's links. The handler now captures the result it is
  deleting and ignores the response if the page has moved on.
- Downloads also check the TTL themselves, so a link stops working at exactly
  15 minutes rather than at the next sweep.

### Rate limiting behind a reverse proxy

- Behind Caddy every request comes from Caddy's container address, so limiting
  on `remote_addr` would make one bucket for the whole world. `ProxyFix` with
  `x_for=1` takes the right-most `X-Forwarded-For` entry, which Caddy sets. A
  client-supplied header sits further left and is ignored. Checked live: 11
  requests from one host, each with a different forged header, still hit 429.
- The limiter lives in process memory, so gunicorn runs one worker (with
  threads). More workers would each keep their own count.

### The shared droplet

- **The Caddy container reads a stale Caddyfile.** It bind-mounts a single
  file, which pins the inode it started with, and the host file has since been
  replaced. Editing the host file and reloading changes nothing. Copy it to
  `/config/Caddyfile.next` inside the container, `caddy validate`, then
  `caddy reload --config` that file (see `deploy/README.md`). Back the host file
  up first, and check every site on that Caddy afterwards.
- **Compose names collide on a shared network.** The other projects' compose
  folders are also called `deploy/`, so the default project name `deploy` would
  adopt their containers as orphans, and a second service called `app` would
  make Caddy round-robin between two backends. Hence `name: ibkr-tax-report` and
  a service of the same name.
- **gunicorn 26 opens a control socket under `$HOME`.** With a read-only root
  and a user with no home it logs `Control server error: Read-only file
  system` at every start; serving is unaffected. `control_socket_disable = True`
  (PR #3). New major versions of server software bring new defaults that write
  files; read the startup log of the hardened container, not just the health
  check.
- **Check data-centre egress before relying on it.** Some sites block cloud IP
  ranges. The RBA serves F11.1 to the droplet and to a container on its network
  (checked 2026-10-15), so the weekly refresh runs inside the app.

### Process

- **Copilot reviewed PR #2 within minutes, and all six findings were real.**
  Read `gh pr view N --json reviews` and the inline comments
  (`gh api repos/OWNER/REPO/pulls/N/comments`) before merging.
- **Mutation-test the tests.** Each fix was reverted once to confirm its test
  fails. A test that passes against the bug is decoration.
- **The GitHub merge button authors the merge commit with the profile display
  name**, not the git identity. To keep `chrisudf` as the author, build the
  merge commit locally (`git checkout -b merge-prN origin/main && git merge
  --no-ff feature`) and `git push origin HEAD:main`; GitHub marks the PR merged.
  Doing it on a temporary branch avoids touching a checkout someone else is
  working in.
