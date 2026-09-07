"""Standalone access to the Second Brain (Hermes-facing).

Entry point is `python -m secondbrain.cli`. Nothing is imported eagerly here
so running the module never triggers a double-import `RuntimeWarning`.
"""