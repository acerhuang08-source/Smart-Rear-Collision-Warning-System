"""Command-line generation of the offline competition demonstration."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .render import render_demo_html
from .scenarios import build_demo_bundle

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_HTML = Path("demo/rear-warning-demo.html")
_DEFAULT_TRACE = Path("demo/rear-warning-demo-data.json")


def _git_output(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=_REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot read Git metadata: {exc}") from exc
    return completed.stdout.strip()


def read_git_metadata() -> tuple[str, bool]:
    """Read the current revision and tracked/untracked dirty state."""
    head = _git_output("rev-parse", "HEAD")
    dirty = bool(_git_output("status", "--porcelain", "--untracked-files=normal"))
    return head, dirty


def _resolve_output(path: Path) -> Path:
    return path if path.is_absolute() else _REPOSITORY_ROOT / path


def _atomic_write(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_name = stream.name
        os.replace(temporary_name, path)
        os.chmod(path, 0o644)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rear-warning-demo",
        description=(
            "Generate deterministic fusion scenarios and a standalone offline HTML player."
        ),
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_HTML)
    parser.add_argument("--trace-output", type=Path, default=_DEFAULT_TRACE)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace the explicitly selected output files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(None if argv is None else list(argv))
    html_path = _resolve_output(options.output)
    trace_path = _resolve_output(options.trace_output)
    if html_path == trace_path:
        print("HTML and trace outputs must use different paths", file=sys.stderr)
        return 2
    try:
        if not options.force:
            existing = [path for path in (html_path, trace_path) if path.exists()]
            if existing:
                joined = ", ".join(str(path) for path in existing)
                raise FileExistsError(
                    f"refusing to overwrite existing output file(s): {joined}"
                )
        git_head, git_dirty = read_git_metadata()
        bundle = build_demo_bundle(
            git_head=git_head,
            git_dirty=git_dirty,
            generated_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        trace = json.dumps(
            bundle,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        ) + "\n"
        _atomic_write(trace_path, trace, force=options.force)
        try:
            _atomic_write(
                html_path,
                render_demo_html(bundle),
                force=options.force,
            )
        except Exception:
            if not trace_path.exists():
                raise
            print(
                f"trace created but HTML generation failed: {trace_path}",
                file=sys.stderr,
            )
            raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"rear-warning-demo: {exc}", file=sys.stderr)
        return 1
    print(f"offline_html={html_path}")
    print(f"trace_json={trace_path}")
    print(f"git_head={git_head}")
    print(f"git_dirty={str(git_dirty).lower()}")
    print("hardware_io=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
