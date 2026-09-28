"""TLS trust bootstrap.

Some machines sit behind a TLS-intercepting proxy or antivirus that re-signs
HTTPS traffic with a private root CA. Python's bundled `certifi` roots know
nothing about that CA, so every market-data request fails with
"unable to get local issuer certificate".

`ensure_ca_bundle()` builds a merged PEM (certifi + the Windows trust store)
once, caches it under data/, and points every HTTP stack at it -- including
curl_cffi, which yfinance uses and which ignores Python's `ssl` module.

On machines without interception this is harmless: the merged bundle is just
certifi plus the OS roots.
"""
from __future__ import annotations

import base64
import logging
import os
import ssl
import sys
from pathlib import Path

from app.config import DATA_DIR

log = logging.getLogger(__name__)

BUNDLE_PATH = DATA_DIR / "ca-bundle.pem"
_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def _der_to_pem(der: bytes) -> str:
    b64 = base64.b64encode(der).decode("ascii")
    lines = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n"


def _windows_roots() -> list[str]:
    if sys.platform != "win32":
        return []
    pems: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            for cert, enc, _trust in ssl.enum_certificates(store):
                if enc == "x509_asn":
                    pems.append(_der_to_pem(cert))
        except Exception as exc:
            log.debug("could not read Windows cert store %s: %s", store, exc)
    return pems


def build_bundle(force: bool = False) -> Path:
    """Write (or reuse) the merged CA bundle and return its path."""
    if BUNDLE_PATH.exists() and not force:
        return BUNDLE_PATH

    import certifi

    parts = [Path(certifi.where()).read_text(encoding="utf-8")]
    roots = _windows_roots()
    parts.extend(roots)
    BUNDLE_PATH.write_text("\n".join(parts), encoding="utf-8")
    log.info("wrote CA bundle with %d OS roots -> %s", len(roots), BUNDLE_PATH)
    return BUNDLE_PATH


def ensure_ca_bundle(force: bool = False) -> Path:
    """Build the bundle and export it to every env var the HTTP stacks read."""
    path = build_bundle(force=force)
    for var in _ENV_VARS:
        os.environ[var] = str(path)

    # Belt and braces: make Python's own ssl module use the OS store too.
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass

    return path
