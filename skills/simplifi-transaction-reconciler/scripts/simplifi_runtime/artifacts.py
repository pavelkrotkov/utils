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

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

#: Never follow a symbolic link at the final component. Absent on non-POSIX
#: platforms, where it degrades to the `lstat` check alone rather than an
#: import error.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: What the kernel reports when `O_NOFOLLOW` refuses. Linux says ELOOP;
#: some BSDs say EMLINK or EFTYPE.
_NOFOLLOW_ERRNOS = frozenset(
    value
    for value in (
        errno.ELOOP,
        getattr(errno, "EMLINK", None),
        getattr(errno, "EFTYPE", None),
    )
    if value is not None
)

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


def _data_dir_source(override: str | Path | None = None) -> tuple[str, str] | None:
    """The raw override in force and where it came from, before resolution."""
    if override is not None and str(override).strip():
        return str(override).strip(), "--data-dir"
    from_env = os.environ.get(DATA_DIR_ENV, "").strip()
    if from_env:
        return from_env, f"${DATA_DIR_ENV}"
    return None


def resolve_data_dir(override: str | Path | None = None) -> Path:
    """Pick the data directory without creating it.

    Precedence is explicit argument, then environment, then the default. An
    override is expanded and resolved so that every later comparison — is this
    inside the skill, is this the same file as that one — is between absolute
    paths and cannot be fooled by `..` or a symlink.
    """
    source = _data_dir_source(override)
    if source is not None:
        return Path(source[0]).expanduser().resolve()
    return default_data_dir().resolve()


def prepare_data_dir(path: str | Path | None = None, *, allow_unsafe: bool = False) -> Path:
    """Resolve, create, and vet the data directory.

    Created with mode 0700 directly rather than created-then-chmod-ed: between
    those two calls a world-readable directory exists, and that is exactly the
    window a scheduled run would hit.
    """
    _check_absolute_override(path, allow_unsafe=allow_unsafe)
    directory = resolve_data_dir(path)
    _check_inside_skill(directory, allow_unsafe=allow_unsafe, label="data directory")
    _check_ancestors_replaceable(directory, allow_unsafe=allow_unsafe, label="data directory")
    if not directory.exists():
        directory.mkdir(parents=True, mode=DIR_MODE, exist_ok=True)
    elif not directory.is_dir():
        raise ArtifactError(f"data directory is not a directory: {directory}")
    harden_directory(directory)
    return directory


def _check_absolute_override(path: str | Path | None, *, allow_unsafe: bool) -> None:
    """A relative data directory is the same ambiguity artifacts are refused for.

    `--data-dir data` names a different database every time the working
    directory changes, which for a scheduled job means the transaction and
    decision history quietly forks. Refusing it here keeps the rule about the
    working directory a single rule rather than one that applies to artifacts
    and not to the place they all live.
    """
    source = _data_dir_source(path)
    if source is None:
        return
    raw, origin = source
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return
    _refuse(
        f"{origin} {raw!r} is relative to the current directory, which a "
        f"scheduled run does not control; give an absolute path",
        allow_unsafe=allow_unsafe,
    )


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
    _check_ancestors_replaceable(resolved, allow_unsafe=allow_unsafe, label=label)
    _reject_symlink(resolved, label=label)
    return resolved


def _reject_symlink(path: Path, *, label: str) -> None:
    """Refuse an artifact path whose final component is a symlink.

    Not subject to `--allow-unsafe-paths`, unlike the location rules. Those
    express a preference about where files belong; this one is about whether
    the path we vetted is the file we open. In a sticky world-writable
    directory another user cannot replace our file but can *pre-create* the
    name as a symlink pointing at something we own, and `secure_open`'s
    `O_TRUNC` would then empty that target instead — a shell profile, a key, an
    older database. The check is deliberately narrow: it rejects the leaf, not
    symlinked parent directories, which are ordinary and harmless because the
    ancestor walk vets what they resolve to.

    `O_NOFOLLOW` at open time is the enforcement; this check exists to fail
    with an explanation rather than an `ELOOP` errno, and the two together
    close the window between the check and the open.
    """
    if not path.is_symlink():
        return
    raise ArtifactError(
        f"{label} {path} is a symbolic link; artifacts are written through the "
        f"path given, never through a link that something else controls — "
        f"name the target directly"
    )


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


def _check_ancestors_replaceable(path: Path, *, allow_unsafe: bool, label: str) -> None:
    """Refuse if *any* ancestor lets another user substitute what is beneath it.

    Write permission on a directory is permission to rename and replace the
    entries in it — not only files, but subdirectories. Checking the immediate
    parent alone is therefore not enough: with `/shared` group-writable and
    `/shared/private` a pristine 0700, another user cannot touch a file inside
    `private`, but they can rename `private` aside and put their own directory
    of the same name in its place. The next run then opens their database,
    having verified a 0700 parent that is no longer the one it inspected.

    The sticky bit (as on ``/tmp``) removes exactly that power — entries there
    can only be renamed or removed by their owner — which is why it is
    exempted. Ancestors that do not exist yet are skipped; they will be created
    by us, under a parent this walk has already vetted.
    """
    for ancestor in [path.parent, *path.parent.parents]:
        try:
            mode = stat.S_IMODE(ancestor.stat().st_mode)
        except OSError:
            continue
        if not mode & 0o022 or mode & stat.S_ISVTX:
            continue
        detail = (
            f"its parent directory (mode {mode:04o})"
            if ancestor == path.parent
            else f"its ancestor {ancestor} (mode {mode:04o})"
        )
        _refuse(
            f"{label} {path} sits under a directory other users can write to: "
            f"{detail}; they could substitute it between runs",
            allow_unsafe=allow_unsafe,
        )
        return


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
    if target.is_symlink():
        # Checked before `exists()`, which follows the link and answers False
        # for a dangling one. A chmod here would land on the link's target.
        _reject_symlink(target, label="artifact")
    if not target.exists():
        return
    _require_regular_file(target)
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


def _require_regular_file(path: Path) -> None:
    """Refuse to apply file permissions to something that is not a file.

    A mistyped `--db` naming an existing directory would otherwise be chmod-ed
    to 0600 — stripping its execute bit and making the user's directory
    unusable — before SQLite got as far as reporting that it could not open a
    database there. An ordinary input error must not damage anything.
    """
    if path.is_dir():
        raise ArtifactError(f"{path} is a directory, not an artifact file")
    if not path.is_file():
        raise ArtifactError(f"{path} is not a regular file")


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
    if target.exists() or target.is_symlink():
        harden_existing(target)
        return target
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW, FILE_MODE)
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

    The mode argument to `os.open` applies only when the file is *created*, so
    an artifact that already exists at 0644 keeps 0644 — and `O_TRUNC` would
    start rewriting it while other users can still read along. It is therefore
    tightened before the open, and `fchmod` re-asserts the mode on the open
    descriptor afterwards so the guarantee does not depend on the write
    completing. A rerun of `analyze` over yesterday's world-readable report is
    the ordinary way to reach this.
    """
    target = ensure_parent(path)
    harden_existing(target)
    flags = os.O_WRONLY | os.O_CREAT | _NOFOLLOW
    flags |= os.O_APPEND if "a" in mode else os.O_TRUNC
    try:
        descriptor = os.open(target, flags, FILE_MODE)
    except OSError as exc:
        if getattr(exc, "errno", None) in _NOFOLLOW_ERRNOS and Path(target).is_symlink():
            raise ArtifactError(f"artifact {target} is a symbolic link") from exc
        raise
    try:
        os.fchmod(descriptor, FILE_MODE)
        handle = os.fdopen(descriptor, mode, **kwargs)
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        yield handle


def secure_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    """`Path.write_text` with owner-only permissions."""
    with secure_open(path, "w", encoding=encoding) as handle:
        handle.write(text)
    return Path(path)


def describe_policy(data_dir: Path) -> str:
    """One line for the run log, so the location is never a mystery."""
    return f"data directory {data_dir} (files {FILE_MODE:04o}, directory {DIR_MODE:04o})"
