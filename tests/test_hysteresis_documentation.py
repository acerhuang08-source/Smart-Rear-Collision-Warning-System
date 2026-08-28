"""Stable documentation-status checks for warning hysteresis."""

from __future__ import annotations

from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DOCUMENTS = (
    Path("README.md"),
    Path("CHANGELOG.md"),
    Path("docs/警示鏈架構.md"),
)
_STATUS_MARKERS = (
    "pre-hysteresis",
    "hysteresis status: physical validation pending",
    "distance filtering: disabled",
    "release margin: initial/tunable 0.2 m",
)


@pytest.mark.parametrize("relative_path", _DOCUMENTS)
def test_hysteresis_status_markers_are_documented(
    relative_path: Path,
) -> None:
    content = (_PROJECT_ROOT / relative_path).read_text(encoding="utf-8").lower()

    for marker in _STATUS_MARKERS:
        assert marker in content, f"{relative_path} is missing {marker!r}"
