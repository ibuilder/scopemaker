# Scoping a Windows desktop build

A costed look at shipping ScopeMaker as a desktop application, written because
"is this a Rust/Tauri Windows app yet?" deserves a real answer rather than a
one-line no.

**Status: not started, not committed to.** This is the analysis you would want
before deciding.

**Headline: the thing that made this look hard is no longer true.** WeasyPrint
dropped cairo and GDK-PixBuf; the native surface is now five Pango-chain DLLs.
That moves the honest estimate from "months, don't" to **"2–4 weeks for a real
installer"** — see the risk section for what could still bite.

---

## What is actually being asked

Three different products get called "a desktop app". They are not the same job.

| | What it is | Effort |
|---|---|---|
| **A. Packaged local app** | The existing Flask app, bundled with Python, launched by a native shell that opens a webview at `127.0.0.1`. Same code, same tests. | **2–4 weeks** |
| **B. Local-first single-user app** | A, with accounts, organizations, MFA, invitations and the audit log stripped out. Files, not tenants. | A + 3–5 weeks |
| **C. Rust rewrite** | Reimplement generation, coverage, numbering and paged-media PDF in Rust. | Months. Not recommended. |

Everything below assumes **A**, with notes on what B would additionally cost.

---

## The native dependency question, settled

This was the stated blocker in `ROADMAP.md`, and it was based on stale
information.

WeasyPrint 69 renders PDFs itself via `pydyf`. It has not used cairo for several
major versions. `weasyprint/text/ffi.py` dlopens exactly:

| Library | Required |
|---|---|
| `libgobject-2.0-0` | yes |
| `libpango-1.0-0` | yes |
| `libpangoft2-1.0-0` | yes |
| `libharfbuzz-0` | yes |
| `libfontconfig-1` | yes |
| `libharfbuzz-subset-0` | no — `allow_fail=True` |

No cairo. No GDK-PixBuf. Raster images go through Pillow, which is a pure
Python wheel.

That is five DLLs plus their transitive GLib dependencies (`libglib-2.0`,
`libintl`, `libffi`, `libpcre2`, `zlib`, `libfreetype`, `libexpat`) — the kind
of set you vendor into an installer, not a runtime the user must install first.

This finding has already been applied to the repository: the Docker image, the
CI installer, `docs/deployment.md` and the in-app error message no longer name
cairo or GDK-PixBuf.

**Still to prove:** that the same five load correctly on a clean Windows machine
with no GTK runtime installed, vendored beside the executable. That is the first
spike below, and it is the single fact the whole plan rests on.

---

## What the code says about portability

Measured, not estimated.

**Ports unchanged (~4,000 lines).** The parts that make the product worth
having:

| Module | Lines |
|---|---|
| `models/` | 1,872 |
| `renderers/` | 790 |
| `scope_builder.py` | 615 |
| `coverage.py` | 503 |
| `numbering.py` | 224 |

**Already portable.** SQLite is a first-class target: it is the development
default, foreign keys are enabled explicitly via a `PRAGMA` listener, and the
whole 553-test suite runs on it. No PostgreSQL-only SQL exists — the only
Postgres-specific code is a URL-scheme normaliser for PaaS providers.

**The render queue needs no broker.** It is a database table claimed with a
conditional `UPDATE`, chosen originally so no extra infrastructure was required.
On a desktop, run it inline: `RENDER_ASYNC=0` is already the default.

**Server-coupled surface that option B would remove (~2,400 lines):** auth
routes (580), admin routes (441), account export/deletion (289), accounts (239),
mail (195), MFA (116), metrics (168), Procore (516).

Note that none of this *blocks* option A. A single-user desktop app can run with
one local account and never show a login screen.

---

## Plan for option A

### Phase 0 — Spike: prove the DLLs (2–3 days)

The only phase that can invalidate the rest.

- Vendor the five Pango-chain DLLs beside a Python embeddable distribution.
- On a clean Windows VM with no GTK runtime, render the Division 21 sample and
  compare it byte-for-byte against the CI-produced PDF.
- Confirm font discovery works without a system fontconfig cache — this is the
  most likely failure and the one to find on day two, not week three.

**If this fails,** stop and reconsider. Fallbacks, in order of preference:
ship without PDF and rely on DOCX plus the browser print view; or bundle a
headless Chromium for PDF, which costs ~150 MB and abandons the paged-media
CSS the exhibits are built on.

### Phase 1 — Native shell (1 week)

- Tauri app whose only job is to start the bundled Python, wait for the port,
  and open a webview at `127.0.0.1` on an ephemeral port.
- Bind to loopback only. Generate the `SECRET_KEY` on first run into the user
  profile directory.
- Lifecycle: the Python process dies with the window, and a crashed backend
  surfaces as a dialog rather than a blank webview.

### Phase 2 — Desktop-shaped defaults (1 week)

- SQLite in `%APPDATA%\ScopeMaker`, migrated with Alembic on launch.
- One local account, auto-signed-in; the login screen is never shown.
- `RENDER_ASYNC=0`, rate limiting off, metrics off, mail set to the console
  backend.
- Native file dialogs for export, replacing browser downloads.

### Phase 3 — Distribution (3–5 days)

- MSI or NSIS installer, per-user install so no admin rights are needed.
- **Code signing.** Without an EV certificate, SmartScreen will warn every
  user until reputation accrues. This is a procurement task with a lead time,
  not an engineering one — start it during Phase 0.
- Update channel. Tauri's updater needs a signed manifest and somewhere to host
  it.

### Phase 4 — CI (2–3 days)

- Windows runner building the installer on tags.
- The existing 553 Python and 13 JavaScript tests run unchanged; they do not
  care about the shell.
- A smoke test that launches the built binary, waits for the port, renders a
  PDF, and exits.

**Total: 2–4 weeks** for one person, assuming Phase 0 succeeds.

---

## Risks, honestly ranked

1. **Font rendering differs from CI.** Exhibits are contract documents; if
   Windows substitutes a font, pagination shifts and page counts change. The
   page-fill assertions already in the suite would catch gross breakage, but
   they run on Linux. Mitigation: vendor DejaVu and Liberation with the app and
   pin them in `document.css`, as the Docker image already does.
2. **Code-signing lead time.** Weeks of procurement, and unsigned installers
   get a SmartScreen wall. Start early.
3. **Bundle size.** Python plus WeasyPrint plus fonts lands around 80–120 MB
   before compression. Acceptable for a construction desktop tool; worth
   knowing.
4. **Antivirus false positives** on a bundled interpreter spawning a local
   server. Signing helps.
5. **Two runtimes to keep patched.** Every Python CVE now needs an installer
   release, not a `docker pull`. This is an ongoing cost, not a one-off.

---

## What this buys, and whether it is worth it

**Buys:** offline use on a jobsite with no network; no server for a small
contractor to run; a double-click install; documents that never leave the
machine.

**Costs:** a second distribution channel, signing, slower security patching, and
one more platform for PDF rendering to differ on.

**The honest question** is whether the users are contractors who want to install
software, or firms who want a URL. The application is already built and tested
for the second. If the answer is genuinely the first, option A is a reasonable
2–4 weeks and reuses essentially all of the code.

**Recommendation:** do not commit to the full plan. Spend the 2–3 days on Phase
0. It is cheap, it settles the only question that could sink the project, and
its output — a PDF rendered on Windows with vendored DLLs — is worth having
regardless, because PDF generation is currently unverifiable on Windows at all.
