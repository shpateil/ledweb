"""тест очереди драйвера без железа: проверяем, что в ленту уходят только
настоящие кадры, ни одного пустого, и future резолвится после всех кадров."""
import asyncio
import sys

sys.path.insert(0, "%h/ledweb")
from ledweb import driver as D
from ledweb import protocol as P

sent = []
V = P.VARIANTS["generic"]


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
    print("set_color → кадров в ленте:", len(sent))
    print("  содержит пустой b'':", any(p == b"" for p in sent))
    for p in sent:
        print("   ", p.hex())

    # 2. одиночная команда
    sent.clear()
    await d._send([P.brightness(70, V)])
    print("brightness → кадров:", len(sent), "| hex:", sent[0].hex())

    # 3. пачка из 10 команд подряд — всё должно дойти, ни одного пустого
    sent.clear()
    for i in range(10):
        await d._send([P.color(i, i, i, V)])
    empt = sum(1 for p in sent if p == b"")
    print("10 команд → кадров:", len(sent), "| пустых:", empt)

    # 4. future резолвится только после всех кадров: проверяем счётчик
    print("в _pending после всех команд:", len(d._pending), "(должен быть 0)")

    # 5. порядок сохранён?
    sent.clear()
    seq = [P.color(1, 0, 0, V), P.color(2, 0, 0, V),
           P.color(3, 0, 0, V)]
    for p in seq:
        await d._send([p])
    # в кадре цвета rgb идёт начиная с 4-го байта (7e 00 05 RR GG BB 00 ef)
    got = [p[4] for p in sent]   # 7e 00 05 03 RR GG BB 00 ef
    print("порядок сохранён (r-байты):", got, "ожидалось [1, 2, 3] ->", got == [1, 2, 3])

    d._stop.set()
    serve.cancel()
    print("\nитог:", "ОК" if not any(p == b"" for p in sent) else "ПУСТЫЕ КАДРЫ ЕСТЬ")


if __name__ == "__main__":
    asyncio.run(main())
