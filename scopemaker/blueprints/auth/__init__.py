from flask import Blueprint

bp = Blueprint("auth", __name__, template_folder="templates")

# Imported for their side effect: each module registers routes on `bp`.
# They have to come after the Blueprint exists, hence the E402 exemption.
from . import mfa_routes, routes  # noqa: E402,F401
