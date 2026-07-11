"""Central redaction for errors that cross the agent boundary."""

from __future__ import annotations

import os
import re


_URL_QUERY = re.compile(r"((?:https?|wss?)://[^\s?]+)\?[^\s]+", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"([?&](?:token|api[_-]?key|authorization)=)[^&\s]+",
    re.IGNORECASE,
)


def redact(text: object, *extra_secrets: str | None) -> str:
    """Return display-safe text with known secrets and URL queries removed."""

    value = str(text)
    secrets = [
        os.environ.get("APIFY_TOKEN"),
        os.environ.get("GHOST_WS_TOKEN"),
        *extra_secrets,
    ]
    for secret in secrets:
        if secret:
            value = value.replace(secret, "<redacted>")
    value = _CREDENTIAL.sub(r"\1<redacted>", value)
    return _URL_QUERY.sub(r"\1?<redacted>", value)
