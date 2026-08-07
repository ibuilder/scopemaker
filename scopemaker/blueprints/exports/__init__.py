from flask import Blueprint

bp = Blueprint("exports", __name__)

from . import routes  # noqa: E402,F401
