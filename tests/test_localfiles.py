import pytest

from worker.actionbroker import ActionBroker
from worker.localfiles import LOCAL_FILES_UNAVAILABLE, LocalFiles, LocalFilesUnavailable


def test_direct_adapter_construction_fails_before_touching_paths_or_broker(tmp_path):
    broker = ActionBroker(id_factory=lambda: "must-not-be-used")
    missing_root = tmp_path / "does-not-exist"

    with pytest.raises(LocalFilesUnavailable, match="strong Windows root-confinement"):
        LocalFiles({"workspace": missing_root}, broker)

    assert broker.list() == []
    assert not missing_root.exists()


def test_unavailable_adapter_has_no_file_or_proposal_operations():
    for operation in ("list", "read", "search", "prepare_create", "prepare_edit", "commit"):
        assert not hasattr(LocalFiles, operation)
    assert "local file access is disabled" in LOCAL_FILES_UNAVAILABLE
