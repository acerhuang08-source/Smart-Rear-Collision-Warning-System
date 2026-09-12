"""Safe, injectable persistence for BNO055 calibration profiles."""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, NoReturn, Protocol

from .calibration_profile import (
    PROFILE_JSON_MAX_BYTES,
    BNO055CalibrationProfile,
    BNO055CalibrationProfileError,
    deserialize_calibration_profile,
    serialize_calibration_profile,
    validate_sensor_label,
)


class BNO055ProfileStorageError(OSError):
    """Profile storage could not safely complete an operation."""

    def __init__(
        self,
        message: str,
        *,
        primary: BaseException | None = None,
        cleanup_errors: tuple[BaseException, ...] = (),
    ) -> None:
        super().__init__(message)
        self.primary = primary
        self.cleanup_errors = cleanup_errors


class ProfileFileSystem(Protocol):
    def lstat(self, path: Path) -> os.stat_result: ...
    def open(self, path: Path, flags: int, mode: int = 0o777) -> int: ...
    def fstat(self, fd: int) -> os.stat_result: ...
    def fchmod(self, fd: int, mode: int) -> None: ...
    def fdopen(self, fd: int, mode: str, *, closefd: bool) -> BinaryIO: ...
    def fsync(self, fd: int) -> None: ...
    def close(self, fd: int) -> None: ...
    def link(self, source: Path, target: Path) -> None: ...
    def unlink(self, path: Path) -> None: ...


class _RealProfileFileSystem:
    def lstat(self, path: Path) -> os.stat_result:
        return os.lstat(path)

    def open(self, path: Path, flags: int, mode: int = 0o777) -> int:
        return os.open(path, flags, mode)

    def fstat(self, fd: int) -> os.stat_result:
        return os.fstat(fd)

    def fchmod(self, fd: int, mode: int) -> None:
        os.fchmod(fd, mode)

    def fdopen(self, fd: int, mode: str, *, closefd: bool) -> BinaryIO:
        return os.fdopen(fd, mode, closefd=closefd)

    def fsync(self, fd: int) -> None:
        os.fsync(fd)

    def close(self, fd: int) -> None:
        os.close(fd)

    def link(self, source: Path, target: Path) -> None:
        os.link(source, target, follow_symlinks=False)

    def unlink(self, path: Path) -> None:
        os.unlink(path)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def describe_exception(error: BaseException) -> str:
    """Return a stable detail that never hides an exception's type."""

    message = str(error)
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _raise_storage_failure(
    operation: str,
    primary: BaseException,
    cleanup_errors: list[BaseException],
) -> NoReturn:
    if isinstance(primary, KeyboardInterrupt):
        existing_notes = set(getattr(primary, "__notes__", ()))
        for cleanup_error in cleanup_errors:
            note = f"profile cleanup failed: {describe_exception(cleanup_error)}"
            if note not in existing_notes:
                primary.add_note(note)
                existing_notes.add(note)
        raise primary
    details = [f"primary={describe_exception(primary)}"]
    details.extend(
        f"cleanup={describe_exception(cleanup_error)}"
        for cleanup_error in cleanup_errors
    )
    raise BNO055ProfileStorageError(
        f"{operation} failed: {'; '.join(details)}",
        primary=primary,
        cleanup_errors=tuple(cleanup_errors),
    ) from primary


class BNO055ProfileStore:
    """Load and publish profiles without following or overwriting files."""

    def __init__(
        self,
        *,
        filesystem: ProfileFileSystem | None = None,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(12),
        maximum_input_bytes: int = PROFILE_JSON_MAX_BYTES,
    ) -> None:
        if (
            isinstance(maximum_input_bytes, bool)
            or not isinstance(maximum_input_bytes, int)
            or maximum_input_bytes <= 0
        ):
            raise ValueError("maximum_input_bytes must be a positive integer")
        self._fs = filesystem or _RealProfileFileSystem()
        self._token_factory = token_factory
        self._maximum_input_bytes = maximum_input_bytes

    def _lstat_optional(self, path: Path) -> os.stat_result | None:
        try:
            return self._fs.lstat(path)
        except FileNotFoundError:
            return None

    def _remove_if_owned(
        self, path: Path, identity: os.stat_result, *, description: str
    ) -> None:
        try:
            current = self._fs.lstat(path)
        except FileNotFoundError:
            return
        except BaseException as exc:
            raise BNO055ProfileStorageError(
                f"{description} cleanup status is uncertain: {path} may remain; "
                f"lstat failed with {describe_exception(exc)}"
            ) from exc
        if _same_file(current, identity):
            try:
                self._fs.unlink(path)
            except BaseException as exc:
                raise BNO055ProfileStorageError(
                    f"failed to remove owned {description} {path}: "
                    f"{describe_exception(exc)}"
                ) from exc

    @staticmethod
    def _flags(base: int) -> int:
        return base | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    def save(self, path: str | Path, profile: BNO055CalibrationProfile) -> None:
        target = Path(path)
        if not target.name or target.name in (".", ".."):
            raise BNO055ProfileStorageError("profile output must name a file")
        parent = target.parent
        try:
            parent_info = self._fs.lstat(parent)
        except OSError as exc:
            raise BNO055ProfileStorageError(
                f"profile output directory is unavailable: {parent}; "
                f"primary={describe_exception(exc)}",
                primary=exc,
            ) from exc
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise BNO055ProfileStorageError(
                "profile output parent must be a real directory, not a symlink"
            )
        existing = self._lstat_optional(target)
        if existing is not None:
            kind = "symlink" if stat.S_ISLNK(existing.st_mode) else "existing file"
            raise FileExistsError(f"profile output refuses {kind}: {target}")

        payload = serialize_calibration_profile(profile)
        token = self._token_factory()
        if not isinstance(token, str) or not token or any(
            character not in "0123456789abcdefABCDEF" for character in token
        ):
            raise BNO055ProfileStorageError("temporary-file token is invalid")
        temporary = parent / f".{target.name}.{token}.tmp"
        fd: int | None = None
        directory_fd: int | None = None
        temporary_identity: os.stat_result | None = None
        published_identity: os.stat_result | None = None
        primary: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        try:
            fd = self._fs.open(
                temporary,
                self._flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                0o600,
            )
            temporary_identity = self._fs.fstat(fd)
            if not stat.S_ISREG(temporary_identity.st_mode):
                raise BNO055ProfileStorageError("temporary profile is not a regular file")
            self._fs.fchmod(fd, 0o600)
            stream = self._fs.fdopen(fd, "wb", closefd=False)
            try:
                written = stream.write(payload)
                if written != len(payload):
                    raise BNO055ProfileStorageError(
                        f"short profile write: expected {len(payload)}, got {written}"
                    )
                stream.flush()
            except BaseException as exc:
                primary = exc
            try:
                stream.close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    cleanup_errors.append(exc)
            if primary is not None:
                raise primary
            self._fs.fsync(fd)
            try:
                self._fs.close(fd)
            finally:
                fd = None

            self._fs.link(temporary, target)
            published_identity = temporary_identity
            candidate_identity = self._fs.lstat(target)
            if not _same_file(candidate_identity, temporary_identity):
                raise BNO055ProfileStorageError(
                    "published profile identity does not match temporary file"
                )
            self._fs.unlink(temporary)

            directory_fd = self._fs.open(
                parent,
                self._flags(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)),
            )
            try:
                self._fs.fsync(directory_fd)
            except BaseException as exc:
                primary = exc
            try:
                self._fs.close(directory_fd)
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    cleanup_errors.append(exc)
            finally:
                directory_fd = None
            if primary is not None:
                raise primary
        except BaseException as exc:
            if primary is None:
                primary = exc
            if published_identity is not None:
                try:
                    self._remove_if_owned(
                        target, published_identity, description="final profile"
                    )
                except BaseException as cleanup_exc:
                    cleanup_errors.append(cleanup_exc)
            if temporary_identity is not None:
                try:
                    self._remove_if_owned(
                        temporary, temporary_identity, description="temporary profile"
                    )
                except BaseException as cleanup_exc:
                    cleanup_errors.append(cleanup_exc)
            if fd is not None:
                try:
                    self._fs.close(fd)
                except BaseException as cleanup_exc:
                    cleanup_errors.append(cleanup_exc)
            _raise_storage_failure("profile save", primary, cleanup_errors)

    def load(
        self, path: str | Path, *, expected_sensor_label: str
    ) -> BNO055CalibrationProfile:
        validate_sensor_label(expected_sensor_label)
        target = Path(path)
        try:
            before = self._fs.lstat(target)
        except OSError as exc:
            raise BNO055ProfileStorageError(
                f"profile input is unavailable: {target}; "
                f"primary={describe_exception(exc)}",
                primary=exc,
            ) from exc
        if stat.S_ISLNK(before.st_mode):
            raise BNO055ProfileStorageError("profile input must not be a symlink")
        if not stat.S_ISREG(before.st_mode):
            raise BNO055ProfileStorageError("profile input must be a regular file")
        if before.st_size > self._maximum_input_bytes:
            raise BNO055ProfileStorageError("profile input exceeds size limit")

        fd: int | None = None
        stream: BinaryIO | None = None
        payload: bytes | None = None
        primary: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        try:
            fd = self._fs.open(target, self._flags(os.O_RDONLY))
            opened = self._fs.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or not _same_file(before, opened):
                raise BNO055ProfileStorageError("profile input changed while opening")
            stream = self._fs.fdopen(fd, "rb", closefd=False)
            payload = stream.read(self._maximum_input_bytes + 1)
        except BaseException as exc:
            primary = exc
        if stream is not None:
            try:
                stream.close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    cleanup_errors.append(exc)
        if fd is not None:
            try:
                self._fs.close(fd)
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    cleanup_errors.append(exc)
        if primary is not None:
            _raise_storage_failure(
                f"profile load {target}", primary, cleanup_errors
            )
        if payload is None:
            raise BNO055ProfileStorageError("profile load produced no payload")
        if len(payload) > self._maximum_input_bytes:
            raise BNO055ProfileStorageError("profile input exceeds size limit")
        try:
            profile = deserialize_calibration_profile(payload)
        except BNO055CalibrationProfileError:
            raise
        if profile.sensor_label != expected_sensor_label:
            raise BNO055CalibrationProfileError(
                "sensor_label mismatch: "
                f"expected {expected_sensor_label!r}, got {profile.sensor_label!r}"
            )
        return profile
