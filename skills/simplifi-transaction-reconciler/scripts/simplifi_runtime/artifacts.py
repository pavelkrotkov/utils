"""Where financial artifacts live, and who is allowed to read them.

Every file this runtime produces — the SQLite database, the HTML report, the
model prompts, the proposal CSV, the review packet, the decision ledger —
contains the user's payees, balances, and spending history. Left to argparse
defaults they would land in the current working directory, which is whatever
shell happened to invoke the command, with whatever permissions the umask felt
like. Two failure modes follow from that and neither announces itself:

* The current directory is the installed skill folder, or a git checkout of it.
  Financial data then sits inside a tree that gets committed, synced, shared,
  or wiped and reinstalled.
* The umask is 022, so the file is world-readable. On a shared or managed
  machine that is a disclosure, and nothing in the run will ever mention it.

So location is a decision the runtime makes explicitly rather than inherits.
Bare names resolve inside a data directory outside the skill; paths that are
ambiguous or unsafe are refused rather than silently accepted; and everything
written is owner-only from the moment it exists, not chmod-ed afterwards.

The escape hatch is deliberate and narrow: `--allow-unsafe-paths` (or
``SIMPLIFI_ALLOW_UNSAFE_PATHS=1``) turns the location refusals into warnings.
It does not turn off the permission work — a file being somewhere odd is the
user's business, but a world-readable ledger is never what anyone meant.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

#: Environment overrides. The flag is honoured as well as `--allow-unsafe-paths`
#: so a scheduled run can carry the decision in its own configuration rather
#: than in a command line that has to be edited in two places.
DATA_DIR_ENV = "SIMPLIFI_DATA_DIR"
ALLOW_UNSAFE_ENV = "SIMPLIFI_ALLOW_UNSAFE_PATHS"

#: Owner-only, on both the directory and everything inside it.
DIR_MODE = 0o700
FILE_MODE = 0o600

#: Any bit outside the owner triad. Used for "is this readable by someone else".
NON_OWNER_BITS = 0o077

DEFAULT_DIR_NAME = "simplifi-transaction-reconciler"


class ArtifactError(Exception):
    """A location or permission the runtime declines to accept."""


def skill_root() -> Path:
    """The installed skill directory: ``.../<skill>/scripts/simplifi_runtime``."""
    return Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    """`$SIMPLIFI_DATA_DIR`, else `$XDG_DATA_HOME`, else `~/.local/share`.

    Under the user's home rather than the skill, so reinstalling the skill
    cannot take the ledger with it.
    """
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return (base / DEFAULT_DIR_NAME).expanduser()


def allow_unsafe_from_env() -> bool:
    raw = os.environ.get(ALLOW_UNSAFE_ENV, "").strip().lower()
    return raw not in ("", "0", "false", "no")


def resolve_data_dir(override: str | Path | None = None) -> Path:
    """Pick the data directory without creating it.

    Precedence is explicit argument, then environment, then the default. An
    override is expanded and resolved so that every later comparison — is this
    inside the skill, is this the same file as that one — is between absolute
    paths and cannot be fooled by `..` or a symlink.
    """
    if override is not None and str(override).strip():
        return Path(str(override).strip()).expanduser().resolve()
    from_env = os.environ.get(DATA_DIR_ENV, "").strip()
    if from_env:
        return Path(from_env).expanduser().resolve()
    return default_data_dir().resolve()


def prepare_data_dir(path: str | Path | None = None, *, allow_unsafe: bool = False) -> Path:
    """Resolve, create, and vet the data directory.

    Created with mode 0700 directly rather than created-then-chmod-ed: between
    those two calls a world-readable directory exists, and that is exactly the
    window a scheduled run would hit.
    """
    directory = resolve_data_dir(path)
    if not directory.exists():
        directory.mkdir(parents=True, mode=DIR_MODE, exist_ok=True)
    elif not directory.is_dir():
        raise ArtifactError(f"data directory is not a directory: {directory}")
    _check_inside_skill(directory, allow_unsafe=allow_unsafe, label="data directory")
    harden_directory(directory)
    return directory


def harden_directory(directory: Path) -> None:
    """Make an existing data directory owner-only, or explain why we cannot.

    A directory someone else can write to lets them replace the database with
    one of their choosing, so it is refused outright rather than warned about.
    Tightening only happens on a directory we own; changing the mode of another
    user's directory is not ours to do, and failing loudly is the honest move.
    """
    mode = stat.S_IMODE(directory.stat().st_mode)
    if not mode & NON_OWNER_BITS:
        return
    if not _owned_by_us(directory):
        raise ArtifactError(
            f"data directory {directory} is accessible to other users "
            f"(mode {mode:04o}) and is not owned by you; "
            f"choose a directory you own, or chmod it to {DIR_MODE:04o}"
        )
    directory.chmod(DIR_MODE)


def resolve_artifact(
    value: str | Path,
    data_dir: Path,
    *,
    allow_unsafe: bool = False,
    label: str = "path",
) -> Path:
    """Turn a command-line path into an absolute, vetted artifact location.

    Three cases, and the middle one is the point of this function:

    * A bare name (``simplifi.sqlite``) means "the usual place" — it resolves
      inside the data directory. This is what keeps the defaults working while
      moving where they land.
    * A relative path with separators (``out/report.html``) is ambiguous: it
      names a different file depending on which directory the command ran from,
      and a scheduled run's directory is not something anyone chose. Refused.
    * An absolute path is a location the user actually stated. Accepted after
      the safety checks below.
    """
    text = str(value).strip()
    if not text:
        raise ArtifactError(f"{label} is empty")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        if len(candidate.parts) == 1:
            return data_dir / candidate.name
        _refuse(
            f"{label} {text!r} is relative to the current directory, which a "
            f"scheduled run does not control; give an absolute path or a bare "
            f"filename to place it in {data_dir}",
            allow_unsafe=allow_unsafe,
        )
        return candidate.resolve()
    resolved = _resolve_without_requiring_existence(candidate)
    _check_inside_skill(resolved, allow_unsafe=allow_unsafe, label=label)
    _check_parent_writable_by_others(resolved, allow_unsafe=allow_unsafe, label=label)
    return resolved


def _resolve_without_requiring_existence(path: Path) -> Path:
    """`Path.resolve()` that keeps a not-yet-created leaf.

    The artifact usually does not exist yet — that is the whole point of the
    call that is about to write it — but its parent generally does, and the
    parent is what the safety checks are about.
    """
    parent = path.parent
    if parent.exists():
        return parent.resolve() / path.name
    return path.resolve()


def _check_inside_skill(path: Path, *, allow_unsafe: bool, label: str) -> None:
    root = skill_root()
    if not path.is_relative_to(root):
        return
    _refuse(
        f"{label} {path} is inside the installed skill directory {root}; "
        f"financial artifacts there are committed, synced, or destroyed by a "
        f"reinstall — use a data directory outside the skill",
        allow_unsafe=allow_unsafe,
    )


def _check_parent_writable_by_others(path: Path, *, allow_unsafe: bool, label: str) -> None:
    """Refuse a directory another user can write to, unless it is sticky.

    Write permission on a directory is permission to replace the files in it,
    so a group-writable parent means someone else can substitute the database
    that the next run trusts. The sticky bit (as on ``/tmp``) removes that
    specific power, which is why it is exempted.
    """
    parent = path.parent
    if not parent.exists():
        return
    mode = stat.S_IMODE(parent.stat().st_mode)
    if not mode & 0o022 or mode & stat.S_ISVTX:
        return
    _refuse(
        f"{label} {path} is in a directory other users can write to "
        f"(mode {mode:04o}); they could replace the file between runs",
        allow_unsafe=allow_unsafe,
    )


def _refuse(message: str, *, allow_unsafe: bool) -> None:
    if allow_unsafe:
        _warn(message)
        return
    raise ArtifactError(f"{message} (override with --allow-unsafe-paths)")


def _warn(message: str) -> None:
    import sys

    print(f"WARN {message}", file=sys.stderr)


def _owned_by_us(path: Path) -> bool:
    try:
        return path.stat().st_uid == os.getuid()
    except AttributeError:  # pragma: no cover - non-POSIX
        return True


def harden_existing(path: str | Path) -> None:
    """Bring an artifact that already exists back to owner-only.

    An artifact can predate this policy, or have been restored from a backup
    that flattened its mode. Reading it without a word would make the promise
    that these files are owner-only quietly false, so the permission is checked
    every time the file is used, not only when it is created.
    """
    target = Path(path)
    if not target.exists():
        return
    mode = stat.S_IMODE(target.stat().st_mode)
    if not mode & NON_OWNER_BITS:
        return
    if not _owned_by_us(target):
        raise ArtifactError(
            f"{target} is readable by other users (mode {mode:04o}) and is not "
            f"owned by you; chmod it to {FILE_MODE:04o} before using it"
        )
    target.chmod(FILE_MODE)
    _warn(f"tightened {target} to {FILE_MODE:04o} (was {mode:04o})")


def warn_if_exposed(path: str | Path) -> None:
    """Note an over-permissive *input* file without refusing to read it.

    An exported CSV arrives however the browser or bank wrote it, and its mode
    is not something this runtime chose or should silently change. Saying so is
    useful; failing the ingest over it would only teach people to stop reading
    the output.
    """
    target = Path(path)
    if not target.exists():
        return
    mode = stat.S_IMODE(target.stat().st_mode)
    if mode & NON_OWNER_BITS:
        _warn(f"input {target} is readable by other users (mode {mode:04o})")


def create_private(path: str | Path) -> Path:
    """Ensure the file exists with owner-only permissions, and return it.

    Used for artifacts written by something that opens the path itself —
    SQLite, most notably. Creating it here first means the writer finds an
    existing 0600 file rather than making a fresh one under the ambient umask.
    """
    target = Path(path)
    ensure_parent(target)
    if target.exists():
        harden_existing(target)
        return target
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    os.close(descriptor)
    return target


def ensure_parent(path: str | Path) -> Path:
    """Create the containing directory, owner-only, and return the path."""
    target = Path(path)
    target.parent.mkdir(parents=True, mode=DIR_MODE, exist_ok=True)
    return target


@contextmanager
def secure_open(path: str | Path, mode: str = "w", **kwargs: Any) -> Iterator[IO[Any]]:
    """Open a file for writing, owner-only from the first byte.

    `os.open` with an explicit mode rather than `Path.open` followed by a
    chmod: the latter leaves the file world-readable for the length of the
    write, which for a full report is not a short time.
    """
    target = ensure_parent(path)
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_APPEND if "a" in mode else os.O_TRUNC
    descriptor = os.open(target, flags, FILE_MODE)
    try:
        handle = os.fdopen(descriptor, mode, **kwargs)
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        yield handle
    harden_existing(target)


def secure_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    """`Path.write_text` with owner-only permissions."""
    with secure_open(path, "w", encoding=encoding) as handle:
        handle.write(text)
    return Path(path)


def describe_policy(data_dir: Path) -> str:
    """One line for the run log, so the location is never a mystery."""
    return f"data directory {data_dir} (files {FILE_MODE:04o}, directory {DIR_MODE:04o})"
