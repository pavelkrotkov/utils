"""Load age-encrypted secrets into the process environment.

Design constraints, in order of importance:

1. **Plaintext never touches disk.** `age -d` writes to a pipe; we parse it in
   memory and discard the buffer. There is no temp file at any point.
2. **Fail loudly.** A missing identity, a missing secrets file, or a bad
   passphrase produces a clear error and a non-zero exit — never a silent
   fallback to an unauthenticated call that then 401s confusingly.
3. **Never log a value.** Errors and status lines name the *keys* that were
   loaded, never their contents.

What this buys, honestly: protection against the secrets file leaking — a stray
backup, an accidental `git add`, someone reading over your shoulder. It does
NOT protect against anyone who already has your user or root on the live host,
because the identity file must be readable by this process. The durable security
win remains minimising what is stored at all.

Setup:

    mkdir -p ~/.config/simplifi-transaction-reconciler
    age-keygen -o ~/.config/simplifi-transaction-reconciler/identity.txt
    chmod 600 ~/.config/simplifi-transaction-reconciler/identity.txt
    grep 'public key' ~/.config/helper-txn/identity.txt

    cat <<'EOF' | age -r age1YOURKEY > ~/.config/simplifi-transaction-reconciler/secrets.env.age
    SIMPLIFI_ACCESS_TOKEN=...
    OPENAI_API_KEY=...
    EOF
    chmod 600 ~/.config/simplifi-transaction-reconciler/secrets.env.age
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_DIR = Path.home() / ".config" / "simplifi-transaction-reconciler"
DEFAULT_IDENTITY = DEFAULT_DIR / "identity.txt"
DEFAULT_SECRETS = DEFAULT_DIR / "secrets.env.age"
DECRYPT_TIMEOUT = 30


class SecretsError(RuntimeError):
    """Raised for any failure to obtain secrets. Never carries a secret value."""


def _check_permissions(path: Path) -> str | None:
    """Warn if a secret-bearing file is group- or world-readable."""
    mode = path.stat().st_mode & 0o077
    return f"{path} is mode {oct(path.stat().st_mode & 0o777)}; chmod 600" if mode else None


def decrypt(
    secrets_path: Path | None = None,
    identity_path: Path | None = None,
) -> dict[str, str]:
    """Decrypt and parse the age file. Returns key -> value, in memory only."""
    secrets_path = Path(
        secrets_path or os.environ.get("SIMPLIFI_RECONCILER_SECRETS", DEFAULT_SECRETS)
    )
    identity_path = Path(
        identity_path or os.environ.get("SIMPLIFI_RECONCILER_IDENTITY", DEFAULT_IDENTITY)
    )

    if shutil.which("age") is None:
        raise SecretsError("`age` is not installed (brew install age / apt install age)")
    if not identity_path.exists():
        raise SecretsError(
            f"no age identity at {identity_path}; run age-keygen (see module docstring)"
        )
    if not secrets_path.exists():
        raise SecretsError(f"no secrets file at {secrets_path}")

    if warning := _check_permissions(identity_path):
        raise SecretsError(f"refusing insecure age identity: {warning}")

    try:
        proc = subprocess.run(
            ["age", "-d", "-i", str(identity_path), str(secrets_path)],
            capture_output=True,
            timeout=DECRYPT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SecretsError("age decryption timed out") from exc

    if proc.returncode != 0:
        # stderr from age can name the file but never the plaintext; still, keep
        # it short so nothing unexpected ends up in a log.
        detail = proc.stderr.decode(errors="replace").strip().splitlines()[:2]
        raise SecretsError(f"age failed (exit {proc.returncode}): {' '.join(detail)}")

    values: dict[str, str] = {}
    for raw in proc.stdout.decode().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")

    if not values:
        raise SecretsError(f"{secrets_path} decrypted but contained no KEY=VALUE lines")
    return values


def load_into_env(
    required: list[str] | None = None,
    *,
    secrets_path: Path | None = None,
    identity_path: Path | None = None,
    override: bool = False,
    verbose: bool = False,
) -> list[str]:
    """Populate os.environ from the age file. Returns the key names loaded.

    Existing environment variables win unless `override=True`, so an operator
    can shadow a single secret for a one-off run without editing the vault.
    """
    values = decrypt(secrets_path, identity_path)

    loaded: list[str] = []
    for key, value in values.items():
        if override or not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)

    missing = [k for k in (required or []) if not os.environ.get(k)]
    if missing:
        raise SecretsError(f"missing required secret(s): {', '.join(missing)}")

    if verbose:
        # Key names only. Never values.
        print(
            f"INFO secrets loaded: {', '.join(sorted(loaded)) or '(all already set)'}",
            file=sys.stderr,
        )
        for path in (secrets_path or DEFAULT_SECRETS, identity_path or DEFAULT_IDENTITY):
            p = Path(path)
            if p.exists() and (warning := _check_permissions(p)):
                print(f"WARNING {warning}", file=sys.stderr)
    return loaded
