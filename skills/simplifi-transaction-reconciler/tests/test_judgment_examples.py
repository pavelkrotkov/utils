from pathlib import Path

import pytest
from simplifi_runtime import judgment_examples
from simplifi_runtime.judgment_examples import (
    JudgmentExampleError,
    load_curated_examples,
    select_relevant_examples,
)

EXAMPLES = Path(__file__).parents[1] / "references" / "examples" / "judgment-examples.md"


def test_loader_reads_only_promoted_portable_decisions():
    examples = load_curated_examples(EXAMPLES)

    assert len(examples) == 5
    assert examples[0]["id"] == "judgment-1"
    assert examples[0]["title"] == "Projected versus real subscription"
    assert "many more charges" in examples[0]["situation"]
    assert "account_name" not in str(examples)
    assert "transaction_id" not in str(examples)


def test_selection_is_relevant_and_deterministic():
    examples = load_curated_examples(EXAMPLES)

    selected = select_relevant_examples(examples, "subscription_creep projected pending")

    assert selected[0]["id"] == "judgment-1"
    assert selected == select_relevant_examples(examples, "subscription_creep projected pending")


def test_loader_rejects_sensitive_or_unknown_fields(tmp_path: Path):
    unsafe = tmp_path / "judgment-examples.md"
    unsafe.write_text(
        """# Judgment examples

## 1. Unsafe

**Situation**
An account_id was exposed.

**Evidence**
Safe-looking text.

**Proposal or escalation**
Review it.

**Human decision**
Do not ship it.

**Reusable lesson**
Keep it out.
""",
        encoding="utf-8",
    )

    with pytest.raises(JudgmentExampleError, match="forbidden term"):
        load_curated_examples(unsafe)


def test_loader_rejects_raw_descriptor_language_in_content(tmp_path: Path):
    unsafe = tmp_path / "judgment-examples.md"
    unsafe.write_text(
        """## 1. Unsafe

**Situation**
A raw statement descriptor was copied into the example.

**Evidence**
Safe-looking text.

**Proposal or escalation**
Review it.

**Human decision**
Do not ship it.

**Reusable lesson**
Keep it out.
""",
        encoding="utf-8",
    )

    with pytest.raises(JudgmentExampleError, match="forbidden term"):
        load_curated_examples(unsafe)


def test_loader_rejects_unrecognized_field(tmp_path: Path):
    malformed = tmp_path / "judgment-examples.md"
    malformed.write_text(
        """## 1. Incomplete

**Situation**
Something happened.

**Unknown field**
Not part of the contract.
""",
        encoding="utf-8",
    )

    with pytest.raises(JudgmentExampleError, match="unsupported field"):
        load_curated_examples(malformed)


def test_loader_rejects_duplicate_field_headings(tmp_path: Path):
    duplicate = tmp_path / "judgment-examples.md"
    duplicate.write_text(
        """## 1. Duplicate

**Situation**
First situation.

**Situation**
Second situation.

**Evidence**
Evidence.

**Proposal or escalation**
Review it.

**Human decision**
Keep it.

**Reusable lesson**
Do not overwrite sections.
""",
        encoding="utf-8",
    )

    with pytest.raises(JudgmentExampleError, match="repeats field"):
        load_curated_examples(duplicate)


def test_loader_can_use_the_bundled_runtime_copy(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(judgment_examples, "DEFAULT_EXAMPLES_PATH", tmp_path / "missing.md")

    examples = judgment_examples.load_curated_examples()

    assert len(examples) == 5
    assert examples[1]["title"] == "Statement evidence versus display name"
