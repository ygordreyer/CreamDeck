"""Run review reproductions as assertions against the hardened backend."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_backend import BackendTests, RenameCapabilityTests

CASES = {
    "MANIFEST_ABSOLUTE /etc": BackendTests("test_manifest_absolute_traversal_and_separators_rejected"),
    "MOCKED_PROXY_UNINSTALL_REMOVES": BackendTests("test_malicious_proxy_state_rejected_before_any_delete"),
    "MOCKED_POST_UPDATE_UNINSTALL_DLL": BackendTests("test_post_update_uninstall_keeps_new_dll_and_backup"),
    "DELETE_RACE / RENAME_RACE": BackendTests("test_delete_and_rename_races_preserve_new_bytes"),
    "MISSING_RENAMEAT2": RenameCapabilityTests("test_missing_wrapper_uses_architecture_syscall"),
    "OVERWRITTEN_JOURNAL_ORIGINAL": BackendTests("test_launch_restore_failure_blocks_reinstall_and_retains_original"),
    "CD5_QUARANTINE_COLLISION": BackendTests("test_quarantine_reservation_collision_never_adopts_existing_bytes"),
    "CD5_FAILED_CAPTURE_COLLISION": BackendTests("test_failed_capture_does_not_adopt_occupied_destination"),
    "CD5_JOURNAL_BEFORE_CAPTURE_CRASH": BackendTests("test_crash_between_journal_and_capture_recovers_empty_reservation"),
    "CD5_PARTIAL_PROBE_ENOSPC_EINVAL": BackendTests("test_partial_probe_enospc_preserves_original_error_and_cleans_all_entries"),
}

suite = unittest.TestSuite(CASES.values())
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful():
    sys.exit(1)
for label in CASES:
    print(label + ": unsafe behavior did not reproduce")
