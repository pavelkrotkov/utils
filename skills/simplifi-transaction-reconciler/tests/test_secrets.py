import os
import shutil
import subprocess

import pytest
from simplifi_runtime import secrets


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not available")
def test_insecure_identity_is_rejected_before_decryption(tmp_path, monkeypatch):
    identity = tmp_path / "identity.txt"
    vault = tmp_path / "secrets.env.age"
    identity.write_text("AGE-SECRET-KEY-1...", encoding="utf-8")
    vault.write_text("ciphertext", encoding="utf-8")
    identity.chmod(0o644)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/age")

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("age should not run for an insecure identity")

    monkeypatch.setattr(subprocess, "run", should_not_run)

    with pytest.raises(secrets.SecretsError, match="insecure age identity"):
        secrets.decrypt(vault, identity)
