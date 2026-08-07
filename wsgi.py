"""WSGI entry point.

Run with::

    gunicorn --config gunicorn.conf.py wsgi:app
"""

from __future__ import annotations

import os

from scopemaker import create_app

app = create_app(os.environ.get("FLASK_ENV"))

if __name__ == "__main__":  # pragma: no cover - local development only
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=app.debug)
