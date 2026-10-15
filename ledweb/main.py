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
    drivers: list[Driver] = []
    for st in store.strips():
        # mac пуст — лента не настроена: поднимаем драйвер, но автоподключение
        # выключено, иначе фоновый цикл будет вечно долбить bluez впустую
        dev = Device(
            mac=st.get("mac") or "",
            name=st.get("name") or "лента",
            variant=store.settings.get("variant", P.DEFAULT_VARIANT),
            auto_connect=bool(store.settings.get("auto_connect", True)) and bool(st.get("mac")),
        )
        drivers.append(Driver(dev))
    if not drivers:
        drivers = [Driver(Device())]
    return Server(drivers, store, host, port)


async def _start_drivers(srv: Server) -> None:
    """старт фоновых циклов всех лент. driver.start() возвращается сразу."""
    for d in srv.drivers:
        await d.start()


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
    # legacy-флаги из однойленточных времён: применяются к первой ленте
    if args.mac:
        srv.drivers[0].dev.mac = args.mac
        srv.drivers[0].dev.auto_connect = True
    if args.variant:
        for d in srv.drivers:
            d.dev.variant = args.variant

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    # поднимаем ленты и http параллельно: страница отдаётся сразу, даже если
    # лента ещё не нашлась в эфире — статус всё равно полезен
    http_task = asyncio.create_task(srv.run(), name="http")
    # driver.start() только создаёт фоновую задачу и возвращается сразу,
    # поэтому его нельзя ждать в FIRST_COMPLETED — демон сразу бы сдох.
    # держим ссылку, чтобы отменить при остановке
    boot = asyncio.create_task(_start_drivers(srv), name="ble-boot")
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
    for d in srv.drivers[1:]:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(d.stop(), timeout=10)
    log.info("остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("падение на старте")
        sys.exit(1)
