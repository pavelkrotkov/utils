# ADR-008: Keep generated financial artifacts in a protected external directory

- Status: Accepted
- Scope: where the database, reports, prompts, proposals, and ledgers are
  written, and what permissions they carry

## Context

Every file this runtime produces is derived financial data. The SQLite database
holds the full transaction history; the HTML report and review packet hold
payees, amounts, and account names; the classifier prompt holds the same rows
formatted for a model; the proposal CSV and decision ledger hold the judgments
made about them. None of this is less sensitive than the source export.

Before this decision, all of it inherited two defaults nobody chose:

- **Location came from the working directory.** The shipped defaults were bare
  relative names (`simplifi.sqlite`, `report.html`, `proposals.csv`,
  `review-packet.json`, `decisions.json`), so the artifacts landed wherever the
  command happened to be run. Run from the skill directory — the natural place,
  since that is where the entrypoint lives — and financial data lands inside a
  tree that gets committed to git, synced between machines, or deleted and
  replaced by a reinstall. A scheduled run has no meaningful working directory
  at all.
- **Permissions came from the umask.** Under the common `022` default, a report
  is created world-readable. On a shared host, a managed laptop, or anything
  with a backup agent, that is a disclosure, and nothing in the run's output
  would ever mention it.

Both failures are silent. The run succeeds, the report renders, and the only
evidence is a `ls -l` nobody performs.

## Decision

**Artifacts live in a data directory outside the skill.** It is
`$SIMPLIFI_DATA_DIR`, else `$XDG_DATA_HOME/simplifi-transaction-reconciler`,
else `~/.local/share/simplifi-transaction-reconciler`, and `--data-dir`
overrides all three. It is created mode `0700`; an existing directory that is
more open is tightened if we own it and refused if we do not, because a
directory another user can write to is a directory in which our database can be
replaced.

**A bare filename means "the usual place."** The shipped defaults keep working
unchanged, but resolve inside the data directory rather than the working
directory. This is what makes the safe location the *default* rather than an
opt-in that most runs would skip.

**Ambiguous and unsafe locations are refused, not corrected.** Three refusals:

- A relative path with separators (`out/report.html`) names a different file
  depending on where the command ran. A run cannot be reproduced or audited if
  its outputs move with the shell.
- A path inside the installed skill directory, for the reasons above.
- A path with *any* ancestor other users can write to. Checking the immediate
  parent is not enough: with `/shared` group-writable and `/shared/private` a
  pristine `0700`, nobody can touch a file inside `private`, but they can
  rename `private` aside and leave their own directory in its place — so the
  next run opens their database having verified a parent that no longer exists.
  The sticky bit is exempted, since it removes exactly that power.

Separately, and *not* subject to the override, an artifact path whose final
component is a symbolic link is refused, and every write uses `O_NOFOLLOW`. The
location rules express a preference about where files belong; this one is about
whether the path we vetted is the file we open. In a sticky world-writable
directory another user cannot replace our file, but they can pre-create the
name as a link to something we own, and an `O_TRUNC` write would then empty
that target instead — a shell profile, a key, an older database. The `lstat`
check exists to fail with an explanation rather than an `ELOOP`; `O_NOFOLLOW`
closes the window between the check and the open. The cost is that a
deliberately symlinked report is refused, which is the right trade for an
artifact class where substitution is the threat.

`--allow-unsafe-paths`, or `SIMPLIFI_ALLOW_UNSAFE_PATHS=1`, downgrades these
refusals to warnings. It exists because a user who states an unusual location
deliberately is entitled to it, and a policy with no escape hatch gets worked
around in ways that are worse than the thing it prevented.

**Permissions are not negotiable.** Every artifact is created `0600` by an
`os.open` with an explicit mode, not by a write followed by a `chmod` — the
latter leaves the file world-readable for the length of the write, which for a
full report is not a short time. That mode argument applies only when the file
is *created*, so an artifact that already exists at `0644` keeps `0644`: it is
tightened before the open, and `fchmod` re-asserts the mode on the open
descriptor, so the guarantee holds during the write and does not depend on the
write finishing. Rerunning `analyze` over yesterday's report is the ordinary
way to reach that case. The database is created by us before SQLite
opens it, so its `-wal` and `-shm` sidecars inherit the mode rather than the
umask. The override loosens locations and never permissions: where a ledger
lives is the user's business, but a world-readable ledger is not something
anyone intended.

**Existing artifacts are checked on every use, not only on creation.** An
artifact can predate this policy or come back from a backup that flattened its
mode. If it is over-permissive and we own it, it is tightened and the change is
reported; if we do not own it, the run fails rather than proceeding on a file
it cannot protect.

This applies to the review packet and the proposals file as well as to what we
write. They are artifacts of this workflow rather than foreign inputs, and a
group-writable proposals file is worth failing over: another local user could
edit the judgments in the window between an agent producing them and `decide`
recording them immutably.

**Inputs are reported, not rewritten.** An exported CSV arrives with whatever
mode the browser or bank gave it. Saying so is useful; changing a file the user
did not ask us to manage is not, and failing the ingest over it would only
teach people to stop reading warnings.

## Consequences

Artifacts survive a skill reinstall and stay out of version control. The
default path is stable enough to document, back up, and point a scheduled run
at. A user with an unusual layout has to say so once, explicitly.

Backup and retention follow from this, and are the operator's to decide:

- **The data directory is the backup unit.** It contains the database, which is
  the only artifact that cannot be regenerated — reports, packets, prompts, and
  proposals are all derived from it and can be rebuilt by re-running `analyze`.
  The decision ledger is append-only and is likewise not reconstructible.
- **Backups must preserve permissions,** or restore into a directory that
  already has them. An archive that flattens modes reintroduces exactly the
  exposure this decision removes; the next run will tighten what it touches and
  say so, but only for the files it touches.
- **Retention is bounded by the source, not by us.** The runtime never deletes
  an artifact. Reports and prompts from superseded runs accumulate and can be
  removed freely; the database and the ledger cannot, because provenance and
  decision history are the point of them.
- **The data directory is as sensitive as the account it describes.** Treat it
  the way the source export is treated: no shared drives, no sync services
  without encryption, and no inclusion in a repository.

## Alternatives considered

**Require an explicit `--data-dir` with no default.** Safest, and rejected:
every invocation and every document would carry a path, and the first thing
anyone would do is pick their working directory anyway. A safe default that is
hard to leave beats a mandatory choice that is answered carelessly.

**Keep the working directory and only fix permissions.** This closes the
disclosure but not the loss — data inside the skill tree still disappears on
reinstall and still risks being committed. The two problems have one cause and
were worth fixing together.

**Encrypt the artifacts at rest.** A larger decision with a key-management
problem attached, and orthogonal to this one: a plaintext file with the right
mode is already the property most operating systems are built to enforce.
Encryption remains available as a future layer over the same directory.
