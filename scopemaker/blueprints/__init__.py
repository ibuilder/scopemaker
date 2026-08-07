"""HTTP layer.

Each blueprint is a package whose ``__init__`` declares the Blueprint and then
imports its routes, so ``from .blueprints.auth import bp`` is enough to wire it
up in the application factory.
"""
