from pathlib import Path

import pytest

from worker.capabilities import load_catalog


def test_catalog_covers_requested_domains_and_confirmation_boundary():
    catalog = load_catalog(Path(__file__).parents[1] / "config" / "capabilities.yaml")
    assert {"local_files", "desktop", "browser", "google_drive", "google_docs", "gmail",
            "google_calendar"} <= {
        item.domain for item in catalog.values()
    }
    assert catalog["local_files.edit"].confirmation == "trusted_action"
    assert catalog["google.gmail.send"].confirmation == "trusted_action"
    assert "kb_vm.delegate" not in catalog


def test_rejects_mutating_capability_without_trusted_confirmation(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("capabilities:\n - {id: x, domain: d, operation: send, risk: T3, availability: a, confirmation: none}\n")
    with pytest.raises(ValueError, match="trusted_action"):
        load_catalog(path)
