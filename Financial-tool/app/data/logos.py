"""Instrument logos: fetched once, cached on disk, monogram when unknown.

The rules that shaped this:

  * The page must never wait on, or leak to, a third party at render time.
    Templates point at our own `/logo/{symbol}` route; the app fetches the
    real image once, writes it under data/logos/, and serves it from there
    forever after. The browser never talks to the logo provider.
  * A missing logo is normal, not an error. Bonds, futures proxies and the
    odd ticker have no artwork anywhere, so every symbol falls back to a
    monogram tile generated locally -- deterministic colour, no network.
  * Failures are cached too. A provider that 404s (or is unreachable because
    the machine is offline) is not asked again for a week, so a dead lookup
    costs one request, not one per page view.

Set LOGOS_ENABLED=false to skip the network entirely and use monograms.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time

from app.config import DATA_DIR, settings

log = logging.getLogger(__name__)

LOGO_DIR = DATA_DIR / "logos"

# Tickers only: letters, digits, dot and dash (BRK.B, RDS-A). Anything else is
# someone probing the filesystem through the URL.
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")

# How long a failed lookup stays failed before we ask the provider again.
_MISS_TTL = 7 * 24 * 3600
_TIMEOUT = 8.0
_MAX_BYTES = 512 * 1024

# Content types we are willing to store and serve back.
_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/gif": ".gif",
}
_MEDIA = {v: k for k, v in _EXT.items()}

# One fetch per symbol at a time: a cold dashboard asks for twenty logos at
# once, and an htmx swap can ask again mid-flight.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(symbol: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(symbol, threading.Lock())


def normalise(symbol: str) -> str | None:
    """Uppercase and validate a ticker, or None if it is not one."""
    sym = (symbol or "").strip().upper()
    for suffix in (".PNG", ".SVG", ".JPG", ".WEBP"):
        if sym.endswith(suffix):  # tolerate /logo/AAPL.png
            sym = sym[: -len(suffix)]
            break
    if ".." in sym or not _SYMBOL_RE.match(sym):
        return None
    return sym


# --------------------------------------------------------------------------
# Monogram fallback
# --------------------------------------------------------------------------
def monogram(symbol: str) -> bytes:
    """A coloured tile carrying the first letters of the ticker.

    The hue comes from a hash of the symbol, so a ticker always gets the same
    colour and neighbouring rows rarely collide. Full-bleed on purpose: the
    tile behind real logos is light, and this has to cover it.
    """
    sym = (symbol or "?").upper()
    letters = sym.split(".")[0][:2] or "?"
    hue = int(hashlib.md5(sym.encode("utf-8")).hexdigest()[:8], 16) % 360
    bg = f"hsl({hue} 42% 24%)"
    fg = f"hsl({hue} 78% 74%)"
    size = 34 if len(letters) > 1 else 46
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100' "
        f"role='img' aria-label='{sym}'>"
        f"<rect width='100' height='100' fill='{bg}'/>"
        f"<text x='50' y='50' fill='{fg}' font-size='{size}' font-weight='700' "
        "font-family='Inter,Segoe UI,Helvetica,Arial,sans-serif' "
        "letter-spacing='-1' text-anchor='middle' dominant-baseline='central'>"
        f"{letters}</text></svg>"
    )
    return svg.encode("utf-8")


# --------------------------------------------------------------------------
# Disk cache
# --------------------------------------------------------------------------
def _cached(symbol: str) -> tuple[bytes, str] | None:
    for ext, media in _MEDIA.items():
        path = LOGO_DIR / f"{symbol}{ext}"
        if path.exists():
            try:
                return path.read_bytes(), media
            except OSError:
                return None
    return None


def _miss_is_fresh(symbol: str) -> bool:
    try:
        age = time.time() - (LOGO_DIR / f"{symbol}.miss").stat().st_mtime
    except OSError:
        return False
    return age < _MISS_TTL


def _mark_miss(symbol: str) -> None:
    try:
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        (LOGO_DIR / f"{symbol}.miss").write_bytes(b"")
    except OSError:
        pass


def _store(symbol: str, data: bytes, media: str) -> None:
    try:
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LOGO_DIR / f".{symbol}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, LOGO_DIR / f"{symbol}{_EXT[media]}")
        (LOGO_DIR / f"{symbol}.miss").unlink(missing_ok=True)
    except OSError as exc:
        log.debug("could not cache logo for %s: %s", symbol, exc)


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------
def _verify():
    """Trust the merged CA bundle app.certs built, if there is one."""
    bundle = os.environ.get("SSL_CERT_FILE")
    return bundle if bundle and os.path.exists(bundle) else True


def _download(symbol: str) -> tuple[bytes, str] | None:
    template = (settings.logo_url_template or "").strip()
    if not template:
        return None
    try:
        url = template.format(symbol=symbol, symbol_lower=symbol.lower())
    except (KeyError, IndexError):
        log.warning("LOGO_URL_TEMPLATE has an unknown placeholder: %s", template)
        return None

    import httpx

    try:
        with httpx.Client(timeout=_TIMEOUT, verify=_verify(), follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "TradingDesk/1.0"})
    except Exception as exc:  # offline, DNS, TLS -- all just "no logo today"
        log.debug("logo fetch failed for %s: %s", symbol, exc)
        return None

    media = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if resp.status_code != 200 or media not in _EXT or not resp.content:
        log.debug("logo miss for %s: HTTP %s %s", symbol, resp.status_code, media)
        return None
    if len(resp.content) > _MAX_BYTES:
        log.debug("logo for %s is %d bytes, ignoring", symbol, len(resp.content))
        return None
    return resp.content, media


# --------------------------------------------------------------------------
# What the route calls
# --------------------------------------------------------------------------
def logo(symbol: str) -> tuple[bytes, str, bool]:
    """Return (bytes, media type, is_real) for a ticker. Never raises."""
    sym = normalise(symbol)
    if not sym:
        return monogram(symbol or "?"), "image/svg+xml", False

    hit = _cached(sym)
    if hit:
        return hit[0], hit[1], True

    if not settings.logos_enabled or _miss_is_fresh(sym):
        return monogram(sym), "image/svg+xml", False

    with _lock_for(sym):
        hit = _cached(sym)  # another thread may have won the race
        if hit:
            return hit[0], hit[1], True
        try:
            got = _download(sym)
        except Exception as exc:  # a decoration must not be able to break a page
            log.debug("logo lookup for %s raised: %s", sym, exc)
            got = None
        if got is None:
            _mark_miss(sym)
            return monogram(sym), "image/svg+xml", False
        _store(sym, got[0], got[1])
        return got[0], got[1], True


def prefetch(symbols: list[str]) -> int:
    """Warm the cache for a list of tickers. Returns how many are real logos."""
    return sum(1 for sym in symbols if logo(sym)[2])


def cache_stats() -> dict:
    """What is on disk right now -- shown in Settings."""
    real = miss = 0
    if LOGO_DIR.exists():
        for path in LOGO_DIR.iterdir():
            if not path.is_file():
                continue
            if path.suffix in _MEDIA:
                real += 1
            elif path.suffix == ".miss":
                miss += 1
    return {"real": real, "miss": miss, "dir": str(LOGO_DIR)}


def clear_cache() -> int:
    """Delete every cached logo and miss marker. Returns files removed."""
    removed = 0
    if not LOGO_DIR.exists():
        return 0
    for path in LOGO_DIR.iterdir():
        if path.is_file() and path.suffix in {*_MEDIA, ".miss", ".tmp"}:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed
