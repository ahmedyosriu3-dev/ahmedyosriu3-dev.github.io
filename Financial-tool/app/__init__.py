"""Personal trading tool.

TLS trust is bootstrapped here, before anything imports an HTTP client, so
machines behind a TLS-intercepting proxy can still reach market data.
"""
from app.certs import ensure_ca_bundle

ensure_ca_bundle()
