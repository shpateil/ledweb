"""точка входа ledweb: демон + веб-панель управления лентой elk-bledom.

    python -m ledweb.main --port 8099

корректно гасится по SIGTERM/SIGINT: без этого systemd ждёт TimeoutStopSec
и убивает процесс SIGKILL, а лента остаётся в последнем состоянии.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from . import protocol as P
from .driver import Device, Driver
from .server import Server
from .store import Store

log = logging.getLogger("ledweb")


def build(host: str = "127.0.0.1", port: int = 8123) -> Server:
    store = Store()
    dev = Device(
        mac=store.settings.get("mac", P.DEFAULT_MAC),
        variant=store.settings.get("variant", P.DEFAULT_VARIANT),
        auto_connect=store.settings.get("auto_connect", True),
    )
    return Server(Driver(dev), store, host, port)


async def main() -> None:
    ap = argparse.ArgumentParser(description="ledweb — панель elk-bledom")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--mac", default=None, help="mac ленты")
    ap.add_argument("--variant", default=None, choices=list(P.VARIANTS))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    srv = build(args.host, args.port)
    if args.mac:
        srv.driver.dev.mac = args.mac
    if args.variant:
        srv.driver.dev.variant = args.variant

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    # поднимаем ленту и http параллельно: страница отдаётся сразу, даже если
    # лента ещё не нашлась в эфире — статус всё равно полезен
    http_task = asyncio.create_task(srv.run(), name="http")
    # driver.start() только создаёт фоновую задачу и возвращается сразу,
    # поэтому его нельзя ждать в FIRST_COMPLETED — демон сразу бы сдох.
    # держим ссылку, чтобы отменить при остановке
    boot = asyncio.create_task(srv.driver.start(), name="ble-boot")
    stop_task = asyncio.create_task(stop.wait(), name="stop")

    done, pending = await asyncio.wait(
        {http_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    log.info("останавливаюсь (%s)", ", ".join(t.get_name() for t in done))

    for t in (http_task, boot, stop_task):
        if not t.done():
            t.cancel()
    for t in (http_task, boot):
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t

    # гасим фоновые циклы и отпускаем ленту. на disconnect уходит до 5 секунд
    # (таймаут в драйвере), поэтому заранее поднимаем лимит
    srv.close()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(srv.driver.stop(), timeout=10)
    log.info("остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("падение на старте")
        sys.exit(1)
