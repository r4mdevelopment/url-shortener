import ipaddress
import re
from urllib.parse import urlparse

from fastapi import HTTPException

DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


def validate_public_url(original_url: str) -> None:
    parsed = urlparse(original_url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail="URL must use http or https")

    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="URL must contain a valid host")

    normalized_host = host.rstrip(".").lower()
    if normalized_host in BLOCKED_HOSTS:
        raise HTTPException(status_code=400, detail="Local URLs are not allowed")

    try:
        ip = ipaddress.ip_address(normalized_host)
    except ValueError:
        _validate_domain(normalized_host)
        return

    if not ip.is_global:
        raise HTTPException(status_code=400, detail="Private, local or reserved IP URLs are not allowed")


def _validate_domain(host: str) -> None:
    if len(host) > 253 or "." not in host:
        raise HTTPException(status_code=400, detail="URL host must be a valid domain")
    labels = host.split(".")
    if any(not label for label in labels):
        raise HTTPException(status_code=400, detail="URL host must be a valid domain")
    if not all(DOMAIN_LABEL_RE.match(label) for label in labels):
        raise HTTPException(status_code=400, detail="URL host must be a valid domain")
    tld = labels[-1]
    if len(tld) < 2 or tld.isdigit():
        raise HTTPException(status_code=400, detail="URL host must have a valid top-level domain")
