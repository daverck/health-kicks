from pathlib import Path

import pytest
from app.core.config import load_settings
from app.core.pseudonymization import (
    get_device_pseudonym,
    get_subject_pseudonym,
    mask_email,
)


def test_subject_pseudonym_deterministic() -> None:
    """Verify get_subject_pseudonym is deterministic and distinct per user ID."""
    pseudo_1 = get_subject_pseudonym(42)
    pseudo_2 = get_subject_pseudonym(42)
    pseudo_other = get_subject_pseudonym(43)

    assert pseudo_1 == pseudo_2
    assert pseudo_1.startswith("SUBJ-")
    assert len(pseudo_1) == len("SUBJ-") + 8
    assert pseudo_1 != pseudo_other


def test_device_pseudonym_deterministic() -> None:
    """Verify get_device_pseudonym is deterministic and distinct per device ID."""
    dev_1 = get_device_pseudonym("HK-1")
    dev_2 = get_device_pseudonym("HK-1")
    dev_other = get_device_pseudonym("HK-2")

    assert dev_1 == dev_2
    assert dev_1.startswith("DEV-")
    assert len(dev_1) == len("DEV-") + 8
    assert dev_1 != dev_other


def test_mask_email() -> None:
    """Verify mask_email hides PII while preserving domain hint."""
    assert mask_email("alice@example.com") == "a***@e***.com"
    assert mask_email("bob.smith@company.org") == "b***@c***.org"
    assert mask_email(None) is None
    assert mask_email("invalid-email") is None


def test_production_rejects_insecure_pseudonymization_pepper(tmp_path: Path, monkeypatch) -> None:
    """Verify production strictly requires a dedicated strong pseudonymization pepper."""
    monkeypatch.setenv("HEALTHKICKS_ENVIRONMENT", "production")
    monkeypatch.setenv("HEALTHKICKS_JWT_SECRET", "s" * 32)
    monkeypatch.setenv("HEALTHKICKS_PUBLIC_ORIGINS", "https://app.healthkicks.com")

    # Default dev pepper must be rejected
    monkeypatch.delenv("HEALTHKICKS_PSEUDONYMIZATION_PEPPER", raising=False)
    with pytest.raises(ValueError, match="Insecure pseudonymization pepper"):
        load_settings(tmp_path / "missing.yaml")

    # Short pepper must be rejected
    monkeypatch.setenv("HEALTHKICKS_PSEUDONYMIZATION_PEPPER", "too-short")
    with pytest.raises(ValueError, match="Insecure pseudonymization pepper"):
        load_settings(tmp_path / "missing.yaml")

    # Valid pepper succeeds
    monkeypatch.setenv("HEALTHKICKS_PSEUDONYMIZATION_PEPPER", "p" * 32)
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.pseudonymization_pepper == "p" * 32

