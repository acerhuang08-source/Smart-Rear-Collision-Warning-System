from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

import pytest

from rear_warning.sensors.bno055 import (
    BNO055CalibrationData,
    BNO055CalibrationProfile,
    BNO055CalibrationProfileError,
    BNO055ProfileStorageError,
    BNO055ProfileStore,
    serialize_calibration_profile,
)


def profile() -> BNO055CalibrationProfile:
    return BNO055CalibrationProfile(
        sensor_label="rear-imu-primary",
        chip_id=0xA0,
        accelerometer_id=0xFB,
        magnetometer_id=0x32,
        gyroscope_id=0x0F,
        software_revision=0x0308,
        bootloader_revision=0x15,
        unit_sel=0,
        power_mode=0,
        operation_mode=0x0C,
        axis_map_config=0x24,
        axis_map_sign=0,
        calibration=BNO055CalibrationData(1, -2, 3, -4, 5, -6, 7, -8, 9, 1000, 480),
        created_at_utc="2026-09-08T12:34:56.000000Z",
    )


class StreamWrapper:
    def __init__(self, stream: BinaryIO, filesystem: FileSystem) -> None:
        self.stream = stream
        self.filesystem = filesystem

    def write(self, data: bytes) -> int:
        self.filesystem.raise_if_requested("write_interrupt", KeyboardInterrupt())
        if "partial_write" in self.filesystem.failures:
            return self.stream.write(data[: max(1, len(data) // 2)])
        self.filesystem.raise_if_requested("write", OSError("write failed"))
        return self.stream.write(data)

    def flush(self) -> None:
        self.filesystem.raise_if_requested("flush", OSError("flush failed"))
        self.stream.flush()

    def read(self, size: int = -1) -> bytes:
        self.filesystem.raise_if_requested("read", OSError("read failed"))
        return self.stream.read(size)

    def close(self) -> None:
        self.stream.close()
        self.filesystem.raise_if_requested(
            "stream_close", OSError("stream close failed")
        )


class FileSystem:
    def __init__(self, failures: str | set[str] | None = None) -> None:
        if isinstance(failures, str):
            self.failures = {failures}
        else:
            self.failures = set(failures or ())
        self.link_succeeded = False
        self.linked_identity: os.stat_result | None = None
        self.target_lstat_calls_after_link = 0

    def raise_if_requested(self, name: str, error: BaseException) -> None:
        if name in self.failures:
            raise error

    def lstat(self, path: Path) -> os.stat_result:
        return os.lstat(path)

    def open(self, path: Path, flags: int, mode: int = 0o777) -> int:
        return os.open(path, flags, mode)

    def fstat(self, fd: int) -> os.stat_result:
        return os.fstat(fd)

    def fchmod(self, fd: int, mode: int) -> None:
        self.raise_if_requested("fchmod", OSError("chmod failed"))
        os.fchmod(fd, mode)

    def fdopen(self, fd: int, mode: str, *, closefd: bool) -> BinaryIO:
        return StreamWrapper(os.fdopen(fd, mode, closefd=closefd), self)  # type: ignore[return-value]

    def fsync(self, fd: int) -> None:
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):
            self.raise_if_requested("file_fsync", OSError("file fsync failed"))
        if stat.S_ISDIR(mode):
            self.raise_if_requested(
                "directory_fsync", OSError("directory fsync failed")
            )
        os.fsync(fd)

    def close(self, fd: int) -> None:
        mode = os.fstat(fd).st_mode
        os.close(fd)
        if stat.S_ISREG(mode):
            self.raise_if_requested("fd_close", OSError("fd close failed"))
        if stat.S_ISDIR(mode):
            self.raise_if_requested(
                "directory_close", OSError("directory close failed")
            )

    def link(self, source: Path, target: Path) -> None:
        self.raise_if_requested("publish", OSError("publish failed"))
        os.link(source, target, follow_symlinks=False)
        self.link_succeeded = True
        self.linked_identity = os.lstat(source)

    def unlink(self, path: Path) -> None:
        failure = "temp_unlink" if path.name.startswith(".") else "final_unlink"
        self.raise_if_requested(failure, OSError(f"{failure} failed"))
        os.unlink(path)


class PostLinkLstatFileSystem(FileSystem):
    def __init__(
        self, *, persistent: bool = False, failures: set[str] | None = None
    ) -> None:
        super().__init__(failures)
        self.persistent = persistent

    def lstat(self, path: Path) -> os.stat_result:
        if self.link_succeeded and path.name == "profile.json":
            self.target_lstat_calls_after_link += 1
            if self.persistent or self.target_lstat_calls_after_link == 1:
                raise OSError("post-link lstat failed")
        return super().lstat(path)


class ReplaceTemporaryDuringCleanupFileSystem(FileSystem):
    def lstat(self, path: Path) -> os.stat_result:
        if path.name.startswith(".") and path.exists():
            os.unlink(path)
            path.write_text("external-temp-replacement", encoding="utf-8")
        return super().lstat(path)


class RetainTemporaryUntilCleanupFileSystem(FileSystem):
    def __init__(self, failures: set[str]) -> None:
        super().__init__(failures)
        self.temporary_unlink_calls = 0

    def unlink(self, path: Path) -> None:
        if path.name.startswith("."):
            self.temporary_unlink_calls += 1
            if self.temporary_unlink_calls == 1:
                return
        super().unlink(path)


class LinkFailureWithExternalTargetFileSystem(FileSystem):
    def link(self, source: Path, target: Path) -> None:
        target.write_text("external-target", encoding="utf-8")
        raise OSError("publish failed after external target appeared")


class ReplacePublishedFileSystem(FileSystem):
    def link(self, source: Path, target: Path) -> None:
        super().link(source, target)
        os.unlink(target)
        target.write_text("external-replacement", encoding="utf-8")


def store(
    *, failure: str | set[str] | None = None, token: str = "0123456789abcdef"
) -> BNO055ProfileStore:
    return BNO055ProfileStore(
        filesystem=FileSystem(failure), token_factory=lambda: token
    )


def test_save_is_canonical_mode_0600_and_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "profile.json"
    store().save(target, profile())
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_bytes() == serialize_calibration_profile(profile())
    assert store().load(target, expected_sensor_label="rear-imu-primary") == profile()
    assert list(tmp_path.iterdir()) == [target]


def test_existing_file_and_output_symlink_are_never_overwritten(tmp_path: Path) -> None:
    existing = tmp_path / "existing.json"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        store().save(existing, profile())
    assert existing.read_text(encoding="utf-8") == "keep"
    link = tmp_path / "link.json"
    link.symlink_to(existing)
    with pytest.raises(FileExistsError, match="symlink"):
        store().save(link, profile())
    assert existing.read_text(encoding="utf-8") == "keep"


def test_output_parent_symlink_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(BNO055ProfileStorageError, match="parent"):
        store().save(link / "profile.json", profile())


@pytest.mark.parametrize(
    "failure",
    ["partial_write", "write", "flush", "file_fsync", "publish", "directory_fsync", "fchmod"],
)
def test_write_publish_failures_leave_no_final_or_owned_temp(
    failure: str, tmp_path: Path
) -> None:
    target = tmp_path / "profile.json"
    with pytest.raises((OSError, BNO055ProfileStorageError)):
        store(failure=failure).save(target, profile())
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_post_link_lstat_failure_removes_owned_final_and_temp(tmp_path: Path) -> None:
    target = tmp_path / "profile.json"
    filesystem = PostLinkLstatFileSystem()
    selected = BNO055ProfileStore(
        filesystem=filesystem, token_factory=lambda: "deadbeef"
    )
    with pytest.raises(BNO055ProfileStorageError, match="post-link lstat failed"):
        selected.save(target, profile())
    assert filesystem.linked_identity is not None
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_persistent_post_link_lstat_failure_reports_uncertain_final(
    tmp_path: Path,
) -> None:
    target = tmp_path / "profile.json"
    filesystem = PostLinkLstatFileSystem(persistent=True)
    selected = BNO055ProfileStore(
        filesystem=filesystem, token_factory=lambda: "deadbeef"
    )
    with pytest.raises(BNO055ProfileStorageError) as caught:
        selected.save(target, profile())
    assert "final profile cleanup status is uncertain" in str(caught.value)
    assert str(target) in str(caught.value)
    assert filesystem.linked_identity is not None
    assert target.exists()
    assert os.path.samestat(os.lstat(target), filesystem.linked_identity)
    assert list(tmp_path.iterdir()) == [target]


def test_directory_close_failure_rolls_back_owned_final(tmp_path: Path) -> None:
    target = tmp_path / "profile.json"
    filesystem = FileSystem("directory_close")
    selected = BNO055ProfileStore(
        filesystem=filesystem, token_factory=lambda: "deadbeef"
    )
    with pytest.raises(BNO055ProfileStorageError, match="directory close failed"):
        selected.save(target, profile())
    assert filesystem.linked_identity is not None
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_directory_fsync_primary_is_not_hidden_by_directory_close(
    tmp_path: Path,
) -> None:
    target = tmp_path / "profile.json"
    selected = BNO055ProfileStore(
        filesystem=FileSystem({"directory_fsync", "directory_close"}),
        token_factory=lambda: "deadbeef",
    )
    with pytest.raises(BNO055ProfileStorageError) as caught:
        selected.save(target, profile())
    detail = str(caught.value)
    assert "primary=OSError: directory fsync failed" in detail
    assert "cleanup=OSError: directory close failed" in detail
    assert detail.index("directory fsync failed") < detail.index("directory close failed")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_primary_write_and_stream_close_and_fd_close_are_all_reported(
    tmp_path: Path,
) -> None:
    target = tmp_path / "profile.json"
    selected = BNO055ProfileStore(
        filesystem=FileSystem({"write", "stream_close", "fd_close"}),
        token_factory=lambda: "deadbeef",
    )
    with pytest.raises(BNO055ProfileStorageError) as caught:
        selected.save(target, profile())
    detail = str(caught.value)
    assert "primary=OSError: write failed" in detail
    assert "cleanup=OSError: stream close failed" in detail
    assert "cleanup=OSError: fd close failed" in detail
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_primary_read_and_fd_close_are_both_reported(tmp_path: Path) -> None:
    target = tmp_path / "profile.json"
    target.write_bytes(serialize_calibration_profile(profile()))
    selected = BNO055ProfileStore(filesystem=FileSystem({"read", "fd_close"}))
    original_identity = os.lstat(target)
    with pytest.raises(BNO055ProfileStorageError) as caught:
        selected.load(target, expected_sensor_label="rear-imu-primary")
    detail = str(caught.value)
    assert "primary=OSError: read failed" in detail
    assert "cleanup=OSError: fd close failed" in detail
    assert os.path.samestat(os.lstat(target), original_identity)


def test_publish_primary_and_temp_unlink_cleanup_are_both_reported(
    tmp_path: Path,
) -> None:
    target = tmp_path / "profile.json"
    selected = BNO055ProfileStore(
        filesystem=FileSystem({"publish", "temp_unlink"}),
        token_factory=lambda: "deadbeef",
    )
    with pytest.raises(BNO055ProfileStorageError) as caught:
        selected.save(target, profile())
    detail = str(caught.value)
    assert "primary=OSError: publish failed" in detail
    assert "temporary profile" in detail
    temporary = tmp_path / ".profile.json.deadbeef.tmp"
    assert not target.exists()
    assert temporary.exists()


def test_post_link_primary_and_final_unlink_cleanup_are_both_reported(
    tmp_path: Path,
) -> None:
    target = tmp_path / "profile.json"
    filesystem = PostLinkLstatFileSystem(failures={"final_unlink"})
    selected = BNO055ProfileStore(
        filesystem=filesystem, token_factory=lambda: "deadbeef"
    )
    with pytest.raises(BNO055ProfileStorageError) as caught:
        selected.save(target, profile())
    detail = str(caught.value)
    assert "primary=OSError: post-link lstat failed" in detail
    assert "failed to remove owned final profile" in detail
    assert filesystem.linked_identity is not None
    assert os.path.samestat(os.lstat(target), filesystem.linked_identity)
    assert not (tmp_path / ".profile.json.deadbeef.tmp").exists()


def test_cleanup_does_not_delete_replaced_temporary(tmp_path: Path) -> None:
    target = tmp_path / "profile.json"
    filesystem = ReplaceTemporaryDuringCleanupFileSystem("file_fsync")
    selected = BNO055ProfileStore(
        filesystem=filesystem, token_factory=lambda: "deadbeef"
    )
    with pytest.raises(BNO055ProfileStorageError, match="file fsync failed"):
        selected.save(target, profile())
    temporary = tmp_path / ".profile.json.deadbeef.tmp"
    assert temporary.read_text(encoding="utf-8") == "external-temp-replacement"
    assert not target.exists()


def test_link_failure_never_checks_or_removes_final_as_owned(tmp_path: Path) -> None:
    target = tmp_path / "profile.json"
    filesystem = FileSystem("publish")
    selected = BNO055ProfileStore(
        filesystem=filesystem, token_factory=lambda: "deadbeef"
    )
    with pytest.raises(BNO055ProfileStorageError, match="publish failed"):
        selected.save(target, profile())
    assert not filesystem.link_succeeded
    assert filesystem.target_lstat_calls_after_link == 0
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_link_failure_does_not_remove_external_target(tmp_path: Path) -> None:
    target = tmp_path / "profile.json"
    filesystem = LinkFailureWithExternalTargetFileSystem()
    selected = BNO055ProfileStore(
        filesystem=filesystem, token_factory=lambda: "deadbeef"
    )
    with pytest.raises(BNO055ProfileStorageError, match="publish failed"):
        selected.save(target, profile())
    final_identity = os.lstat(target)
    assert target.read_text(encoding="utf-8") == "external-target"
    assert not filesystem.link_succeeded
    assert os.path.samestat(os.lstat(target), final_identity)
    assert list(tmp_path.iterdir()) == [target]


def test_primary_and_three_cleanup_failures_are_all_preserved(
    tmp_path: Path,
) -> None:
    target = tmp_path / "profile.json"
    filesystem = RetainTemporaryUntilCleanupFileSystem(
        {"directory_fsync", "directory_close", "final_unlink", "temp_unlink"}
    )
    selected = BNO055ProfileStore(
        filesystem=filesystem, token_factory=lambda: "deadbeef"
    )
    with pytest.raises(BNO055ProfileStorageError) as caught:
        selected.save(target, profile())
    detail = str(caught.value)
    assert "primary=OSError: directory fsync failed" in detail
    assert "cleanup=OSError: directory close failed" in detail
    assert "failed to remove owned final profile" in detail
    assert "failed to remove owned temporary profile" in detail
    assert filesystem.temporary_unlink_calls == 2
    assert filesystem.linked_identity is not None
    temporary = tmp_path / ".profile.json.deadbeef.tmp"
    assert os.path.samestat(os.lstat(target), filesystem.linked_identity)
    assert os.path.samestat(os.lstat(temporary), filesystem.linked_identity)


def test_keyboard_interrupt_is_preserved_and_owned_temp_is_removed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "profile.json"
    selected = BNO055ProfileStore(
        filesystem=FileSystem("write_interrupt"), token_factory=lambda: "deadbeef"
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        selected.save(target, profile())
    assert str(caught.value) == ""
    assert getattr(caught.value, "__notes__", []) == []
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_keyboard_interrupt_preserves_all_cleanup_failure_details(
    tmp_path: Path,
) -> None:
    target = tmp_path / "profile.json"
    selected = BNO055ProfileStore(
        filesystem=FileSystem(
            {"write_interrupt", "stream_close", "fd_close", "temp_unlink"}
        ),
        token_factory=lambda: "deadbeef",
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        selected.save(target, profile())
    notes = getattr(caught.value, "__notes__", [])
    assert any("OSError: stream close failed" in note for note in notes)
    assert any("OSError: fd close failed" in note for note in notes)
    assert any("temporary profile" in note and "temp_unlink failed" in note for note in notes)
    assert len(notes) == 3
    assert not target.exists()
    assert (tmp_path / ".profile.json.deadbeef.tmp").exists()


def test_preexisting_temp_collision_is_not_deleted(tmp_path: Path) -> None:
    target = tmp_path / "profile.json"
    collision = tmp_path / ".profile.json.deadbeef.tmp"
    collision.write_text("owned-by-someone-else", encoding="utf-8")
    with pytest.raises(BNO055ProfileStorageError, match="FileExistsError"):
        store(token="deadbeef").save(target, profile())
    assert collision.read_text(encoding="utf-8") == "owned-by-someone-else"
    assert not target.exists()


def test_publish_identity_race_never_deletes_replacement(tmp_path: Path) -> None:
    target = tmp_path / "profile.json"
    filesystem = ReplacePublishedFileSystem()
    selected = BNO055ProfileStore(
        filesystem=filesystem, token_factory=lambda: "deadbeef"
    )
    with pytest.raises(BNO055ProfileStorageError, match="identity"):
        selected.save(target, profile())
    assert filesystem.linked_identity is not None
    assert not os.path.samestat(os.lstat(target), filesystem.linked_identity)
    assert target.read_text(encoding="utf-8") == "external-replacement"
    assert list(tmp_path.iterdir()) == [target]


def test_input_symlink_directory_and_oversize_are_rejected(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    valid.write_bytes(serialize_calibration_profile(profile()))
    link = tmp_path / "link.json"
    link.symlink_to(valid)
    with pytest.raises(BNO055ProfileStorageError, match="symlink"):
        store().load(link, expected_sensor_label="rear-imu-primary")
    with pytest.raises(BNO055ProfileStorageError, match="regular"):
        store().load(tmp_path, expected_sensor_label="rear-imu-primary")
    too_large = tmp_path / "large.json"
    too_large.write_bytes(b"x" * 101)
    small_store = BNO055ProfileStore(maximum_input_bytes=100)
    with pytest.raises(BNO055ProfileStorageError, match="size"):
        small_store.load(too_large, expected_sensor_label="rear-imu-primary")


@pytest.mark.parametrize("payload", [b"\xff", b"not json", b"{}"])
def test_utf8_and_json_errors_are_rejected(payload: bytes, tmp_path: Path) -> None:
    target = tmp_path / "bad.json"
    target.write_bytes(payload)
    with pytest.raises(BNO055CalibrationProfileError):
        store().load(target, expected_sensor_label="rear-imu-primary")


def test_sensor_label_mismatch_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "profile.json"
    target.write_bytes(serialize_calibration_profile(profile()))
    with pytest.raises(BNO055CalibrationProfileError, match="sensor_label mismatch"):
        store().load(target, expected_sensor_label="different")
