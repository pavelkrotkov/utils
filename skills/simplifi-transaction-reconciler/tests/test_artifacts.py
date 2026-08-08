"""Where artifacts land, and who can read them once they are there."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from simplifi_runtime import artifacts
from simplifi_runtime.store import Store


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- the default location ---------------------------------------------------


def test_the_default_data_directory_is_outside_the_installed_skill(monkeypatch, tmp_path):
    monkeypatch.delenv(artifacts.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))

    resolved = artifacts.resolve_data_dir()

    assert not resolved.is_relative_to(artifacts.skill_root())
    assert resolved == (tmp_path / "share" / artifacts.DEFAULT_DIR_NAME).resolve()


def test_the_default_falls_back_to_the_home_data_directory(monkeypatch, tmp_path):
    monkeypatch.delenv(artifacts.DATA_DIR_ENV, raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert (
        artifacts.default_data_dir() == tmp_path / ".local" / "share" / artifacts.DEFAULT_DIR_NAME
    )


def test_an_explicit_argument_outranks_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, str(tmp_path / "from-env"))

    assert artifacts.resolve_data_dir(tmp_path / "explicit") == (tmp_path / "explicit").resolve()


def test_the_data_directory_is_created_owner_only(tmp_path):
    resolved = artifacts.prepare_data_dir(tmp_path / "nested" / "data")

    assert resolved.is_dir()
    assert mode_of(resolved) == artifacts.DIR_MODE


def test_an_existing_open_data_directory_is_tightened(tmp_path):
    exposed = tmp_path / "data"
    exposed.mkdir(mode=0o755)

    artifacts.prepare_data_dir(exposed)

    assert mode_of(exposed) == artifacts.DIR_MODE


def test_a_data_directory_inside_the_skill_is_refused(tmp_path):
    inside = artifacts.skill_root() / "scratch-data"

    with pytest.raises(artifacts.ArtifactError, match="installed skill directory"):
        artifacts.prepare_data_dir(inside)

    assert not inside.exists() or not any(inside.iterdir())


def test_a_file_where_the_data_directory_should_be_is_refused(tmp_path):
    occupied = tmp_path / "data"
    occupied.write_text("not a directory")

    with pytest.raises(artifacts.ArtifactError, match="not a directory"):
        artifacts.prepare_data_dir(occupied)


# --- resolving individual artifacts -----------------------------------------


def test_a_bare_name_lands_in_the_data_directory(tmp_path):
    resolved = artifacts.resolve_artifact("simplifi.sqlite", tmp_path)

    assert resolved == tmp_path / "simplifi.sqlite"


def test_a_relative_path_with_separators_is_refused_as_ambiguous(tmp_path):
    with pytest.raises(artifacts.ArtifactError, match="relative to the current directory"):
        artifacts.resolve_artifact("out/report.html", tmp_path)


def test_an_ambiguous_relative_path_is_allowed_under_the_override(tmp_path, capsys):
    resolved = artifacts.resolve_artifact("out/report.html", tmp_path, allow_unsafe=True)

    assert resolved.is_absolute()
    assert "WARN" in capsys.readouterr().err


def test_an_absolute_path_outside_the_skill_is_accepted(tmp_path):
    target = tmp_path / "elsewhere" / "report.html"
    target.parent.mkdir(mode=artifacts.DIR_MODE)

    assert artifacts.resolve_artifact(target, tmp_path) == target


def test_an_absolute_path_inside_the_skill_is_refused(tmp_path):
    target = artifacts.skill_root() / "report.html"

    with pytest.raises(artifacts.ArtifactError, match="installed skill directory"):
        artifacts.resolve_artifact(target, tmp_path)


def test_a_path_in_a_world_writable_directory_is_refused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)  # `mkdir(mode=...)` is masked by the umask; chmod is not

    with pytest.raises(artifacts.ArtifactError, match="other users can write to"):
        artifacts.resolve_artifact(shared / "simplifi.sqlite", tmp_path)


def test_a_sticky_world_writable_directory_is_accepted(tmp_path):
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    sticky.chmod(0o1777)

    assert artifacts.resolve_artifact(sticky / "db", tmp_path) == sticky / "db"


def test_an_empty_path_is_refused(tmp_path):
    with pytest.raises(artifacts.ArtifactError, match="empty"):
        artifacts.resolve_artifact("   ", tmp_path)


def test_a_dotdot_path_is_resolved_before_the_skill_check(tmp_path):
    """`..` must not be a way to walk back into the skill directory."""
    inside = artifacts.skill_root() / "scripts" / ".." / "sneaky.html"

    with pytest.raises(artifacts.ArtifactError, match="installed skill directory"):
        artifacts.resolve_artifact(inside, tmp_path)


# --- permissions on what gets written ---------------------------------------


def test_a_written_file_is_owner_only(tmp_path):
    target = artifacts.secure_write_text(tmp_path / "report.html", "<html></html>")

    assert mode_of(target) == artifacts.FILE_MODE
    assert target.read_text() == "<html></html>"


def test_a_written_file_is_owner_only_despite_a_permissive_umask(tmp_path):
    previous = os.umask(0o000)
    try:
        target = artifacts.secure_write_text(tmp_path / "report.html", "x")
    finally:
        os.umask(previous)

    assert mode_of(target) == artifacts.FILE_MODE


def test_secure_open_truncates_an_existing_file(tmp_path):
    target = tmp_path / "proposals.csv"
    artifacts.secure_write_text(target, "old and long")

    with artifacts.secure_open(target, "w", newline="", encoding="utf-8") as handle:
        handle.write("new")

    assert target.read_text() == "new"
    assert mode_of(target) == artifacts.FILE_MODE


def test_secure_open_rewrites_an_exposed_existing_file(tmp_path):
    target = tmp_path / "proposals.csv"
    target.write_text("old")
    target.chmod(0o644)

    with artifacts.secure_open(target, "w", encoding="utf-8") as handle:
        handle.write("new")

    assert mode_of(target) == artifacts.FILE_MODE


def test_create_private_makes_an_owner_only_file(tmp_path):
    target = artifacts.create_private(tmp_path / "nested" / "simplifi.sqlite")

    assert mode_of(target) == artifacts.FILE_MODE
    assert mode_of(target.parent) == artifacts.DIR_MODE


def test_an_existing_exposed_artifact_is_tightened_before_use(tmp_path, capsys):
    target = tmp_path / "simplifi.sqlite"
    target.write_text("data")
    target.chmod(0o666)

    artifacts.harden_existing(target)

    assert mode_of(target) == artifacts.FILE_MODE
    assert "tightened" in capsys.readouterr().err


def test_hardening_a_missing_file_is_not_an_error(tmp_path):
    artifacts.harden_existing(tmp_path / "absent")


def test_an_artifact_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    target = tmp_path / "simplifi.sqlite"
    target.write_text("data")
    target.chmod(0o644)
    monkeypatch.setattr(artifacts, "_owned_by_us", lambda path: False)

    with pytest.raises(artifacts.ArtifactError, match="not owned by you"):
        artifacts.harden_existing(target)


def test_an_exposed_input_is_reported_but_not_changed(tmp_path, capsys):
    source = tmp_path / "export.csv"
    source.write_text("date,payee\n")
    source.chmod(0o644)

    artifacts.warn_if_exposed(source)

    assert mode_of(source) == 0o644
    assert "readable by other users" in capsys.readouterr().err


def test_a_private_input_is_reported_silently(tmp_path, capsys):
    source = tmp_path / "export.csv"
    artifacts.secure_write_text(source, "date,payee\n")

    artifacts.warn_if_exposed(source)

    assert capsys.readouterr().err == ""


# --- the database -----------------------------------------------------------


def test_a_new_database_is_owner_only(tmp_path):
    db = tmp_path / "simplifi.sqlite"
    Store(db).close()

    assert mode_of(db) == artifacts.FILE_MODE


def test_an_existing_exposed_database_is_tightened_on_open(tmp_path):
    db = tmp_path / "simplifi.sqlite"
    Store(db).close()
    db.chmod(0o644)

    Store(db).close()

    assert mode_of(db) == artifacts.FILE_MODE


def test_database_sidecars_inherit_owner_only_permissions(tmp_path):
    db = tmp_path / "simplifi.sqlite"
    store = Store(db)
    store.conn.execute("PRAGMA journal_mode = WAL")
    store.start_run("csv", "detail")
    store.commit()
    try:
        for sidecar in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            if sidecar.exists():
                assert mode_of(sidecar) == artifacts.FILE_MODE
    finally:
        store.close()


# --- the override -----------------------------------------------------------


def test_the_environment_override_is_honoured(monkeypatch):
    monkeypatch.setenv(artifacts.ALLOW_UNSAFE_ENV, "1")
    assert artifacts.allow_unsafe_from_env() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "  "])
def test_falsey_override_values_do_not_enable_it(monkeypatch, value):
    monkeypatch.setenv(artifacts.ALLOW_UNSAFE_ENV, value)
    assert artifacts.allow_unsafe_from_env() is False


def test_the_override_does_not_relax_permissions(tmp_path):
    """Location is the user's call; a world-readable ledger never is."""
    target = artifacts.resolve_artifact("out/report.html", tmp_path, allow_unsafe=True)
    artifacts.secure_write_text(target, "<html></html>")

    assert mode_of(target) == artifacts.FILE_MODE


# --- the commands, end to end -----------------------------------------------

FIXTURE_CSV = Path(__file__).parent / "fixtures" / "acceptance.csv"


def run(arguments: list[str]) -> int:
    from simplifi_runtime.cli import build_parser

    args = build_parser().parse_args(arguments)
    return args.func(args)


def test_bare_names_land_in_the_data_directory(tmp_path, monkeypatch):
    """The shipped defaults keep working; they simply stop using the cwd."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, str(data_dir))
    monkeypatch.chdir(tmp_path)

    assert run(["ingest", "--source", "csv", str(FIXTURE_CSV)]) == 0

    assert (data_dir / "simplifi.sqlite").exists()
    assert not (tmp_path / "simplifi.sqlite").exists()


#: Distinct uncategorized payees, so merchant memory cannot resolve any of them
#: and `classify` actually has residue to write a prompt about.
UNRESOLVED_CSV = (
    "Date,Account,Reviewed,Payee,Category,Attachments,Exclusion,Recurring,Amount\n"
    '"Jun 1, 2026",Checking,No,Aurora Bakery,,,No,No,-12.40\n'
    '"Jun 2, 2026",Checking,No,Halcyon Hardware,,,No,No,-31.00\n'
    '"Jun 3, 2026",Checking,No,Meridian Optics,,,No,No,-88.75\n'
)


def test_every_generated_artifact_is_owner_only(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, str(data_dir))
    export = tmp_path / "export.csv"
    artifacts.secure_write_text(export, UNRESOLVED_CSV)

    assert run(["ingest", "--source", "csv", str(export)]) == 0
    assert run(["analyze", "--today", "2026-06-15"]) == 0

    produced = [
        data_dir / "simplifi.sqlite",
        data_dir / "report.html",
        data_dir / "review-packet.json",
    ]
    for path in produced:
        assert path.exists(), path
        assert mode_of(path) == artifacts.FILE_MODE, path


def test_model_prompts_are_owner_only(tmp_path, monkeypatch):
    """A prompt file carries the same payees and amounts the report does.

    A CSV export states no settlement, so no row is statistics-eligible and
    `classify` normally has nothing to write. Eligibility is relaxed here to
    reach the write path — the subject of the test is the file's mode, not the
    rule that decides which rows reach a model.
    """
    from simplifi_runtime import cli

    data_dir = tmp_path / "data"
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setattr(cli, "is_statistics_eligible", lambda row: True)
    export = tmp_path / "export.csv"
    artifacts.secure_write_text(export, UNRESOLVED_CSV)

    assert run(["ingest", "--source", "csv", str(export)]) == 0
    assert run(["classify", "--dry-run"]) == 0

    prompt = data_dir / "proposals.prompt.txt"
    assert prompt.exists()
    assert mode_of(prompt) == artifacts.FILE_MODE


def test_an_unsafe_database_location_is_refused_before_anything_is_written(tmp_path, capsys):
    inside = artifacts.skill_root() / "refused.sqlite"

    assert run(["ingest", "--source", "csv", str(FIXTURE_CSV), "--db", str(inside)]) == 2

    assert "installed skill directory" in capsys.readouterr().err
    assert not inside.exists()


def test_the_override_permits_an_otherwise_refused_location(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)

    code = run(
        [
            "ingest",
            "--source",
            "csv",
            str(FIXTURE_CSV),
            "--db",
            "nested/simplifi.sqlite",
            "--allow-unsafe-paths",
        ]
    )

    assert code == 0
    assert "WARN" in capsys.readouterr().err
    assert mode_of(tmp_path / "nested" / "simplifi.sqlite") == artifacts.FILE_MODE


def test_an_exposed_csv_input_is_reported_without_failing_the_ingest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, str(tmp_path / "data"))
    export = tmp_path / "export.csv"
    export.write_bytes(FIXTURE_CSV.read_bytes())
    export.chmod(0o644)

    assert run(["ingest", "--source", "csv", str(export)]) == 0

    assert "readable by other users" in capsys.readouterr().err
    assert mode_of(export) == 0o644


def test_the_decision_ledger_is_owner_only(tmp_path):
    """The ledger is append-only, so a wrong mode on it is not fixable later."""
    from simplifi_runtime import decisions

    target = tmp_path / "nested" / "decisions.json"
    decisions.write_decisions({"document_type": "test", "records": []}, target)

    assert mode_of(target) == artifacts.FILE_MODE
    assert mode_of(target.parent) == artifacts.DIR_MODE


def test_the_proposals_csv_is_owner_only(tmp_path):
    with artifacts.secure_open(tmp_path / "proposals.csv", "w", newline="", encoding="utf-8") as fh:
        fh.write("transaction_id,proposed_category\r\ntxn-1,Groceries\r\n")

    assert mode_of(tmp_path / "proposals.csv") == artifacts.FILE_MODE


# --- review findings, each with the failure it would have allowed -----------


def test_rewriting_an_exposed_artifact_is_private_during_the_write(tmp_path):
    """`os.open`'s mode applies only on creation, so a rerun kept the old mode."""
    target = tmp_path / "report.html"
    target.write_text("yesterday's report")
    target.chmod(0o644)

    with artifacts.secure_open(target, "w", encoding="utf-8") as handle:
        assert mode_of(target) == artifacts.FILE_MODE  # mid-write, not after
        handle.write("today's report")

    assert mode_of(target) == artifacts.FILE_MODE


def test_a_failed_write_still_leaves_an_owner_only_file(tmp_path):
    target = tmp_path / "report.html"
    target.write_text("old")
    target.chmod(0o666)

    with pytest.raises(RuntimeError), artifacts.secure_open(target, "w", encoding="utf-8") as fh:
        fh.write("partial")
        raise RuntimeError("render failed")

    assert mode_of(target) == artifacts.FILE_MODE


def test_a_symlinked_artifact_is_refused(tmp_path):
    """A pre-created link in a sticky directory must not redirect `O_TRUNC`."""
    victim = tmp_path / "important"
    victim.write_text("do not truncate me")
    link = tmp_path / "report.html"
    link.symlink_to(victim)

    with pytest.raises(artifacts.ArtifactError, match="symbolic link"):
        artifacts.resolve_artifact(link, tmp_path)
    with (
        pytest.raises(artifacts.ArtifactError, match="symbolic link"),
        artifacts.secure_open(link, "w", encoding="utf-8"),
    ):
        pass

    assert victim.read_text() == "do not truncate me"


def test_a_dangling_symlink_is_refused(tmp_path):
    link = tmp_path / "simplifi.sqlite"
    link.symlink_to(tmp_path / "nowhere")

    with pytest.raises(artifacts.ArtifactError, match="symbolic link"):
        artifacts.create_private(link)


def test_a_writable_grandparent_is_refused(tmp_path):
    """A 0700 parent is no protection if its own parent can be renamed away."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    private = shared / "private"
    private.mkdir(mode=artifacts.DIR_MODE)

    with pytest.raises(artifacts.ArtifactError, match="ancestor"):
        artifacts.resolve_artifact(private / "simplifi.sqlite", tmp_path)


def test_a_sticky_ancestor_is_accepted(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o1777)
    private = shared / "private"
    private.mkdir(mode=artifacts.DIR_MODE)

    assert artifacts.resolve_artifact(private / "db", tmp_path) == private / "db"


def test_a_data_directory_under_a_writable_ancestor_is_refused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)

    with pytest.raises(artifacts.ArtifactError, match="other users can write to"):
        artifacts.prepare_data_dir(shared / "data")


def test_a_directory_named_as_an_artifact_is_not_chmodded(tmp_path):
    """A mistyped `--db` must not strip a directory's execute bit."""
    directory = tmp_path / "reports"
    directory.mkdir(mode=0o755)

    with pytest.raises(artifacts.ArtifactError, match="is a directory"):
        artifacts.create_private(directory)

    assert mode_of(directory) == 0o755


def test_a_relative_data_dir_override_is_refused(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(artifacts.ArtifactError, match="relative to the current directory"):
        artifacts.prepare_data_dir("data")


def test_a_relative_data_dir_environment_value_is_refused(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, "data")

    with pytest.raises(artifacts.ArtifactError, match="relative to the current directory"):
        artifacts.prepare_data_dir()


def test_a_relative_data_dir_is_allowed_under_the_override(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)

    resolved = artifacts.prepare_data_dir("data", allow_unsafe=True)

    assert resolved == (tmp_path / "data").resolve()
    assert "WARN" in capsys.readouterr().err


def test_decide_refuses_an_exposed_proposals_file(tmp_path, monkeypatch, capsys):
    """Another local user could edit judgments between proposal and record."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setattr(artifacts, "_owned_by_us", lambda path: False)
    data_dir.mkdir(mode=artifacts.DIR_MODE)
    for name in ("simplifi.sqlite", "review-packet.json", "proposals.json"):
        (data_dir / name).write_text("{}")
        (data_dir / name).chmod(0o666)

    assert run(["decide"]) == 2
    assert "not owned by you" in capsys.readouterr().err


def test_decide_tightens_its_inputs_before_reading_them(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, str(data_dir))
    data_dir.mkdir(mode=artifacts.DIR_MODE)
    for name in ("simplifi.sqlite", "review-packet.json", "proposals.json"):
        (data_dir / name).write_text("{}")
        (data_dir / name).chmod(0o644)

    run(["decide"])  # fails later on content; the permission work is the subject

    assert mode_of(data_dir / "review-packet.json") == artifacts.FILE_MODE
    assert mode_of(data_dir / "proposals.json") == artifacts.FILE_MODE
