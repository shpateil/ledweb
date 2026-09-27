"""тест сторожа и таймаутов без железа.

проверяем главное: сторож не рвёт соединение в покое, но рвёт когда записи
виснут. и что _open() не может висеть вечно.
"""
import asyncio
import sys
import time

sys.path.insert(0, "%h/ledweb")
from ledweb import driver as D

ok = 0
bad = []


def check(name, cond, got=""):
    global ok
    if cond:
        ok += 1
        print(f"  ок   {name}" + (f"  ({got})" if got else ""))
    else:
        bad.append(name)
        print(f"  ПРОВАЛ {name}  ({got})")


class FakeClient:
    def __init__(self, hang_notify=False):
        self.is_connected = True
        self.hang_notify = hang_notify
        self.disconnected = False

    async def start_notify(self, *a):
        if self.hang_notify:
            await asyncio.sleep(3600)      # вот это и вешало демон
        return None

    async def disconnect(self):
        self.disconnected = True
        self.is_connected = False


class Fake(D.Driver):
    def __init__(self):
        self._stop = asyncio.Event()
        self._task = None
        self._watch = None
        self._queue = asyncio.Queue()
        self._pending = {}
        self._ready = asyncio.Event()
        self._client = None
        self.connected = False
        self.last_error = None
        self._last_write = time.monotonic()
        self._notify = []
        self._emit_called = 0
        self.state = type("S", (), {"updated_at": 0})()
        self.on_change = None          # вызывается из _emit(), в фейке нужен
        self.dev = type("Dev", (), {"mac": "AA:BB:CC:DD:EE:FF", "auto_connect": True,
                                    "sync_time": False})()
        self.variant = None
    _watchdog = D.Driver._watchdog
    _teardown = D.Driver._teardown


async def main():
    print("── сторож в покое не трогает ──")
    d = Fake()
    d._client = FakeClient()
    d.connected = True
    d._ready.set()
    d._watch = asyncio.create_task(d._watchdog())
    # прокручиваем 3 интервала сторожа, но очередь пуста и запись была недавно
    D.WATCHDOG_INTERVAL = 0.05
    D.WATCHDOG_IDLE = 5.0
    D.WATCHDOG_STUCK = 1.0
    await asyncio.sleep(0.3)
    check("соединение живо в покое", d._client is not None and d.connected)
    d._stop.set()
    d._watch.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await d._watch

    print("\n── сторож рвёт зависшую запись ──")
    d2 = Fake()
    d2._client = FakeClient()
    d2.connected = True
    d2._ready.set()
    d2._queue.put_nowait((None, b"\x01\x02"))   # коман висит, записи нет
    d2._last_write = time.monotonic() - 99     # давно не было успешной записи
    d2._watch = asyncio.create_task(d2._watchdog())
    await asyncio.sleep(0.4)
    check("соединение сорвано", d2._client is None, f"client={d2._client}")
    check("connected сброшен", d2.connected is False)
    check("очередь не тронута", not d2._queue.empty())
    d2._stop.set()
    d2._watch.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await d2._watch

    print("\n── start_notify с таймаутом не вешает _open ──")
    class Fake2(Fake):
        pass
    d3 = Fake()
    # подменяем только connect и write, реальный код _open проверит notify
    async def fake_connect():
        return FakeClient(hang_notify=True)
    D.BleakClient = lambda mac, timeout=10: type(
        "C", (), {"is_connected": True,
                  "connect": staticmethod(fake_connect),
                  "start_notify": staticmethod(
                      lambda self, *a: asyncio.sleep(3600)),
                  "disconnect": staticmethod(lambda self: asyncio.sleep(0))})()
    d3.dev.sync_time = False
    d3.on_change = None        # _open дергает колбэк, в фейке его нет
    # NOTIFY_TIMEOUT ставим маленький, чтобы тест был быстрым
    D.NOTIFY_TIMEOUT = 0.2
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(d3._open(), timeout=3.0)
        elapsed = time.monotonic() - t0
        check("_open вернулся, не завис", True, f"{elapsed:.2f} с")
    except asyncio.TimeoutError:
        check("_open вернулся, не завис", False, "висит дольше 3 с")
    except Exception as e:
        check("_open вернулся, не завис", False, f"{type(e).__name__}: {e}")

    import importlib
    real = importlib.reload(D)   # выше константы менялись ради скорости
    print("\n── константы на месте ──")
    for name, val in [("GATT_WRITE_TIMEOUT", 2.0), ("NOTIFY_TIMEOUT", 3.0),
                      ("OPEN_TIMEOUT", 45.0), ("WATCHDOG_INTERVAL", 20.0),
                      ("WATCHDOG_STUCK", 30.0), ("WATCHDOG_IDLE", 120.0)]:
        check(f"{name} = {val}", getattr(real, name, None) == val, getattr(real, name, None))

    print(f"\nитог: {ok} ок, {len(bad)} провалов")
    if bad:
        print("провалились: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
