"""Manual, hardware-free browser dashboard entry point."""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path
from typing import Sequence

from rear_warning.dashboard import CaptureDashboardSource
from rear_warning.dashboard.server import DashboardServer


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="SHADOW 中文瀏覽器儀表板（不開感測硬體）")
    result.add_argument("--capture-root", type=Path, default=Path("shadow-results"))
    result.add_argument("--source", choices=("LIVE", "REPLAY", "ANY"), default="LIVE")
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--history-limit", type=int, default=120)
    result.add_argument("--voice-config", type=Path, default=Path("config/voice-alert.local.toml"))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    options = parser().parse_args(None if argv is None else list(argv))
    try:
        address = ipaddress.ip_address(options.host)
    except ValueError as exc:
        parser().error(str(exc))
    if not address.is_loopback:
        parser().error("本入口只允許 loopback 位址")
    if not 1 <= options.port <= 65535 or options.history_limit <= 0:
        parser().error("port／history-limit 無效")
    source = CaptureDashboardSource(
        options.capture_root,
        mode=options.source,
        history_limit=options.history_limit,
        voice_config=options.voice_config if options.voice_config.is_file() else None,
    )
    source.poll_once()
    source.start()
    server = DashboardServer((options.host, options.port), source)
    print("實驗資料顯示／未啟用實體警示")
    print(f"URL=http://{options.host}:{options.port}/")
    print("本程序只讀 SHADOW 檔案；不開 UART／I²C。Ctrl+C 停止。")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
        source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
