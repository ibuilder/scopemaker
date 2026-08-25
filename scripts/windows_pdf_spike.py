"""Prove WeasyPrint can render a PDF on Windows from vendored DLLs.

This is Phase 0 of ``docs/desktop-app-scope.md`` -- the spike the whole desktop
plan is gated on. The question it answers is narrow and load-bearing:

    Do WeasyPrint's native libraries work when placed *beside the application*,
    with no GTK runtime installed on the machine?

If yes, a desktop build is a packaging exercise. If no, the plan needs its
fallback (ship without PDF, or bundle a headless browser and abandon the
paged-media CSS the exhibits are built on).

Usage
-----

    python scripts/windows_pdf_spike.py --dll-dir C:\\path\\to\\dlls

It reports each library individually rather than failing on the first one,
because the usual failure is a missing *transitive* dependency -- ``libpango``
loading fine while ``libgobject`` cannot find ``libintl`` -- and a single
"cannot load library" tells you nothing about which.

Where to get the DLLs
---------------------

WeasyPrint 69 renders PDFs itself and does **not** use cairo or GDK-PixBuf. It
dlopens five libraries (plus ``harfbuzz-subset``, which is optional). Those five
pull in a chain of GLib dependencies.

From MSYS2 (https://mirror.msys2.org/mingw/mingw64/), take these packages and
copy every ``.dll`` out of their ``mingw64/bin`` into one directory:

    mingw-w64-x86_64-pango
    mingw-w64-x86_64-harfbuzz
    mingw-w64-x86_64-fontconfig
    mingw-w64-x86_64-glib2
    mingw-w64-x86_64-freetype
    mingw-w64-x86_64-fribidi
    mingw-w64-x86_64-cairo          (a Pango dependency, not used by WeasyPrint)
    mingw-w64-x86_64-libpng
    mingw-w64-x86_64-expat
    mingw-w64-x86_64-bzip2
    mingw-w64-x86_64-zlib
    mingw-w64-x86_64-libiconv
    mingw-w64-x86_64-gettext
    mingw-w64-x86_64-pcre2
    mingw-w64-x86_64-libffi
    mingw-w64-x86_64-graphite2
    mingw-w64-x86_64-brotli

Installing the GTK3 runtime and pointing ``--dll-dir`` at its ``bin`` also works
and is the quicker way to get an answer -- but it does not prove the vendoring
model, which is the point of the spike. Use it only as a control.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: What weasyprint/text/ffi.py actually dlopens, in load order.
REQUIRED = [
    ("libgobject-2.0-0", "GLib object system"),
    ("libpango-1.0-0", "text layout"),
    ("libpangoft2-1.0-0", "FreeType backend for Pango"),
    ("libharfbuzz-0", "text shaping"),
    ("libfontconfig-1", "font discovery"),
]
OPTIONAL = [("libharfbuzz-subset-0", "font subsetting (allow_fail=True)")]


def probe(names: list[tuple[str, str]]) -> list[tuple[str, str, str | None]]:
    """Try each library on its own and report the real error for each."""
    from cffi import FFI

    ffi = FFI()
    results = []
    for name, purpose in names:
        error = None
        # Only the spellings WeasyPrint itself tries. Inventing variants makes
        # the failure message name a library nobody was ever looking for.
        for candidate in (name, f"{name}.dll"):
            try:
                ffi.dlopen(candidate)
                error = None
                break
            except OSError as exc:
                error = str(exc).split("\n")[0]
        results.append((name, purpose, error))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dll-dir",
        help="Directory holding the vendored DLLs. Omit to test the machine as-is.",
    )
    parser.add_argument(
        "--out",
        default="spike-windows.pdf",
        help="Where to write the rendered exhibit.",
    )
    args = parser.parse_args()

    if sys.platform != "win32":
        print("This spike only means anything on Windows.")
        return 1

    print(f"Python  : {sys.version.split()[0]}")

    if args.dll_dir:
        dll_dir = Path(args.dll_dir).resolve()
        if not dll_dir.is_dir():
            print(f"No such directory: {dll_dir}")
            return 1
        count = len(list(dll_dir.glob("*.dll")))
        print(f"DLL dir : {dll_dir}  ({count} DLLs)")
        # add_dll_directory is the supported way; PATH alone stopped working for
        # extension modules in Python 3.8+.
        os.add_dll_directory(str(dll_dir))
        os.environ["PATH"] = f"{dll_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    else:
        print("DLL dir : (none -- testing the machine as it is)")

    print("\n-- native libraries " + "-" * 52)
    failures = 0
    for name, purpose, error in probe(REQUIRED):
        if error:
            failures += 1
            print(f"  FAIL  {name:24} {purpose}")
            print(f"        {error}")
        else:
            print(f"  ok    {name:24} {purpose}")
    for name, purpose, error in probe(OPTIONAL):
        print(f"  {'ok  ' if not error else 'skip'}  {name:24} {purpose}")

    if failures:
        print(f"\n{failures} required librar{'y' if failures == 1 else 'ies'} "
              "could not be loaded. WeasyPrint will not render.")
        print("A library that fails here is usually missing a *transitive* "
              "dependency rather than being absent itself -- check the chain "
              "listed in this file's docstring.")
        return 1

    print("\n-- weasyprint " + "-" * 58)
    try:
        import weasyprint

        print(f"  imported, version {weasyprint.__version__}")
    except Exception as exc:
        print(f"  FAIL  import: {type(exc).__name__}: {exc}")
        return 1

    print("\n-- rendering " + "-" * 59)
    from scopemaker import create_app
    from scopemaker.extensions import db
    from scopemaker.models import Scope
    from scopemaker.services.renderers import render_pdf

    app = create_app("development")
    with app.app_context():
        scope = db.session.query(Scope).first()
        if scope is None:
            print("  No scope in the database. Run:")
            print("    flask create-user you@example.com --org demo --role admin")
            print("    flask demo-data --org demo")
            return 1

        payload = render_pdf(scope, organization=scope.organization)
        out = Path(args.out).resolve()
        out.write_bytes(payload)

        from pypdf import PdfReader

        pages = len(PdfReader(out).pages)
        print(f"  scope   : {scope.document_title}")
        print(f"  written : {out}  ({len(payload) // 1024} KB, {pages} pages)")

    print("\nPASS -- WeasyPrint rendered a PDF on Windows.")
    print("Compare it against the CI-produced sample for the same division "
          "before calling the spike settled: fonts are the likeliest thing to "
          "differ, and a substituted font changes pagination.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
