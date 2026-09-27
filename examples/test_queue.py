"""тест очереди драйвера без железа: проверяем, что в ленту уходят только
настоящие кадры, ни одного пустого, и future резолвится после всех кадров."""
import asyncio
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# bleak не нужен для этих проверок, но драйвер импортирует его на верхнем
# уровне. подменяем заглушкой, чтобы тесты шли на голом питоне без venv
try:
    import bleak  # noqa: F401
except ImportError:
    import types
    _bleak = types.ModuleType("bleak")
    _bleak.BleakClient = object
    _bleak.BleakScanner = object
    _exc = types.ModuleType("bleak.exc")
    for _name in ("BleakDBusError", "BleakDeviceNotFoundError", "BleakError"):
        setattr(_exc, _name, type(_name, (Exception,), {}))
    _bleak.exc = _exc
    sys.modules["bleak"] = _bleak
    sys.modules["bleak.exc"] = _exc

from ledweb import driver as D
from ledweb import protocol as P

sent = []
V = P.VARIANTS["generic"]
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


class Fake:
    """драйвер без ble: перехватываем всё, что уходит в ленту."""
    variant = V
    last_command_ms = 0.0
    last_error = None

    def __init__(self):
        self._queue = asyncio.Queue()
        self._pending = {}
        self._ready = asyncio.Event()
        self._ready.set()
        self._stop = asyncio.Event()
        self.state = type("S", (), {"updated_at": 0})()
        self.connected = True
        self.on_change = None
        self._client = type("C", (), {"is_connected": True})()

    # переносим реальные методы разбора очереди, подменяя только _write
    _serve = D.Driver._serve
    _teardown = D.Driver._teardown
    _send = D.Driver._send
    _await_ready = D.Driver._await_ready
    _emit = D.Driver._emit

    async def _write(self, payload):
        await asyncio.sleep(0.001)
        sent.append(payload)
        self.last_command_ms = 1.0


async def main():
    d = Fake()
    serve = asyncio.create_task(d._serve())

    # 1. команда из двух кадров: future резолвится и оба кадра в ленте
    sent.clear()
    await d._send([P.single_color(0, V), P.color(1, 2, 3, V)])
    check("set_color: оба кадра дошли", len(sent) == 2, f"кадров {len(sent)}")
    check("set_color: ни одного пустого", not any(p == b"" for p in sent))
    for p in sent:
        print("   ", p.hex())

    # 2. одиночная команда
    sent.clear()
    await d._send([P.brightness(70, V)])
    check("brightness: один кадр", len(sent) == 1, f"кадров {len(sent)}")
    check("brightness: hex не пустой", sent[0].hex() if sent else "кадра нет")

    # 3. пачка из 10 команд подряд — всё должно дойти, ни одного пустого
    sent.clear()
    for i in range(10):
        await d._send([P.color(i, i, i, V)])
    empt = sum(1 for p in sent if p == b"")
    check("10 команд: все дошли", len(sent) == 10, f"кадров {len(sent)}")
    check("10 команд: пустых нет", empt == 0, f"пустых {empt}")

    # 4. future резолвится только после всех кадров: проверяем счётчик
    check("_pending пуст после всех команд", len(d._pending) == 0,
          f"в _pending {len(d._pending)}")

    # 5. порядок сохранён?
    sent.clear()
    seq = [P.color(1, 0, 0, V), P.color(2, 0, 0, V),
           P.color(3, 0, 0, V)]
    for p in seq:
        await d._send([p])
    # в кадре цвета rgb идёт начиная с 4-го байта (7e 00 05 RR GG BB 00 ef)
    got = [p[4] for p in sent]   # 7e 00 05 03 RR GG BB 00 ef
    check("порядок сохранён (r-байты)", got == [1, 2, 3], f"{got} ожидалось [1, 2, 3]")

    d._stop.set()
    serve.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serve

    print(f"\nитог: {ok} ок, {len(bad)} провалов")
    if bad:
        print("провалились: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
