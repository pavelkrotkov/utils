"""Redact credential fields and token-shaped text before logging."""

import re

SENSITIVE = re.compile(
    r"password|passphrase|psk|token|secret|credential|privatekey|networkkey|config|certificate",
    re.I,
)
TOKEN = re.compile(r"(?:oauth2_4/|oauth2rt_1/|aas_et/|ya29\.|Bearer )[^\s\"']+")


def redact(value):
    """Remove credential fields and token-looking substrings recursively."""
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if SENSITIVE.search(re.sub(r"[^a-zA-Z]", "", key))
            or key.lower() in {"key", "authorization"}
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return TOKEN.sub("[redacted]", value) if isinstance(value, str) else value
