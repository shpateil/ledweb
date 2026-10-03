"""тест сторожа и таймаутов без железа.

проверяем главное: сторож не рвёт соединение в покое, но рвёт когда записи
виснут. и что _open() не может висеть вечно.
"""
import asyncio
import importlib
import sys
import time

sys.path.insert(0, "/home/shpateil/ledweb")
from ledweb import driver as D
from ledweb import protocol as P

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
        self._inflight = 0
        self._pushed_to_strip = False
        self._power_known = False
        self._ready = asyncio.Event()
        self._client = None
        self.connected = False
        self.last_error = None
        self._last_write = time.monotonic()
        self._notify = []
        self._emit_called = 0
        # настоящий State: _replay_state читает power/color/brightness/effect
        self.state = D.State(power=True, color=(255, 255, 255), brightness=100,
                             effect=int(importlib.import_module(
                                 "ledweb.protocol").Effect.fade_all))
        self.on_change = None          # вызывается из _emit(), в фейке нужен
        self.dev = type("Dev", (), {"mac": "AA:BB:CC:DD:EE:FF", "auto_connect": True,
                                    "sync_time": False})()
        # настоящий Variant: _replay_state строит по нему кадры
        self.variant = importlib.import_module("ledweb.protocol").VARIANTS["generic"]
    _watchdog = D.Driver._watchdog
    _probe = D.Driver._probe
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


    real = importlib.reload(D)   # выше константы менялись ради скорости
    print("\n── константы на месте ──")
    for name, val in [("GATT_WRITE_TIMEOUT", 2.0), ("NOTIFY_TIMEOUT", 3.0),
                      ("OPEN_TIMEOUT", 45.0), ("WATCHDOG_INTERVAL", 20.0),
                      ("WATCHDOG_STUCK", 30.0), ("WATCHDOG_IDLE", 120.0)]:
        check(f"{name} = {val}", getattr(real, name, None) == val, getattr(real, name, None))

    print("\n── регресс: простой не рвёт соединение ──")
    # ровно тот баг, из-за которого лента каждые ~140 с уходила в свой
    # дефолтный режим: пустая очередь + простой больше WATCHDOG_IDLE
    # проскакивали первый continue, и следующий if проверял простой > STUCK
    d4 = Fake()
    d4._client = FakeClient()
    d4.connected = True
    d4._ready.set()
    d4._queue = asyncio.Queue()        # пусто — записи нет и не было
    d4._inflight = 0
    d4._last_write = time.monotonic() - 9999   # простоя больше любых порогов
    D.WATCHDOG_INTERVAL = 0.05
    # порог выше простоя: этот тест про «простой не рвёт», пробу тут ждать
    # не надо. было ровно 9999 против 9999 с — тест стоял на границе и падал
    D.WATCHDOG_PROBE = 99999.0
    d4._watch = asyncio.create_task(d4._watchdog())
    await asyncio.sleep(0.35)           # 7 интервалов сторожа
    check("простой не рвёт соединение", d4._client is not None and d4.connected,
          f"client={d4._client}")
    d4._stop.set()
    d4._watch.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await d4._watch
    importlib.reload(D)

    print("\n── регресс: счётчик в полёте не залипает ──")
    # декремент стоял в двух точках и только на ветке без исключения. любое
    # другое исключение из _write оставляло _inflight выше нуля навсегда,
    # сторож решал что запись зависла, и демон возвращался к рвению живого
    # соединения — то есть к исходному багу
    class FakeServe(Fake):
        _serve = D.Driver._serve
        _write = D.Driver._write
        _teardown = D.Driver._teardown

    d5 = FakeServe()
    d5._client = None
    d5._inflight = 0
    d5._queue = asyncio.Queue()
    d5._pending = {}
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    d5._pending[fut] = 1
    d5._queue.put_nowait((fut, b"\x7e\x00"))
    calls = {"n": 0}

    async def exploding_write(payload):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("не BleakError")     # счётчик обязан упасть
        if not fut.done():
            fut.set_result(True)
    d5._write = exploding_write
    with __import__("contextlib").suppress(RuntimeError):
        await d5._serve()
    check("RuntimeError из _write не оставил счётчик в полёте",
          d5._inflight == 0, f"_inflight={d5._inflight}")

    print("\n── регресс: восстановление роняет связь, а не врёт ──")
    # если связь мертва, _open() обязан получить исключение, а не вернуться
    # с мёртвым клиентом: иначе он уходит в _serve() и крутится в цикле
    d6 = Fake()
    d6._pushed_to_strip = True
    d6.state.color = (1, 2, 3)
    d6._client = None

    async def dead_write(payload):
        raise D.BleakError("запись зависла, переподключаюсь")
    d6._write = dead_write
    raised = False
    try:
        await d6._replay_state()
    except D.BleakError:
        raised = True
    check("мёртвая связь поднимает ошибку в _open", raised)
    check("счётчик в полёте обнулён после ошибки", d6._inflight == 0,
          f"_inflight={d6._inflight}")

    # связь жива, кадр не ушёл — подключение не рвём
    d7 = Fake()
    d7._pushed_to_strip = True
    d7._client = FakeClient()

    async def flaky_write(payload):
        raise D.BleakError("как один кадр не ушёл")
    d7._write = flaky_write
    check("живая связь: восстановление не рвёт подключение",
          (await d7._replay_state()) is False)
    check("счётчик в полёте обнулён", d7._inflight == 0, f"_inflight={d7._inflight}")

    print("\n── регресс: реплей не гасит ленту ──")
    # power в кэше — просто дефолт False, его никогда не читают с ленты.
    # без флага _power_known реконнект отправлял P.power_off ленте, которую
    # юзер не выключал: жмём только цвет, power остаётся False
    d8 = Fake()
    d8._pushed_to_strip = True
    d8._power_known = False
    d8.state.power = False
    d8.written = []

    async def collect(payload):
        d8.written.append(payload)
    d8._write = collect
    await d8._replay_state()
    check("питание не отправляется без явной команды",
          all(p != P.power(True, d8.variant) for p in d8.written)
          and all(p not in (P.power(True, d8.variant), P.power(False, d8.variant))
                  for p in d8.written),
          [p.hex() for p in d8.written])

    # жались «включить» — теперь питание подтверждено и его можно вернуть
    d9 = Fake()
    d9._pushed_to_strip = True
    d9._power_known = True
    d9.state.power = True
    d9.written = []

    async def collect2(payload):
        d9.written.append(payload)
    d9._write = collect2
    await d9._replay_state()
    check("подтверждённое питание возвращается",
          d9.written and d9.written[0] == P.power(True, d9.variant),
          d9.written[0].hex() if d9.written else "ничего не отправил")

    print("\n── регресс: реплей не затирает эффект ──")
    # set_color сам гасит эффект, значит цвет и эффект несовместимы.
    # при effect != none отправка цвета сбивала бы режим в статичный
    d10 = Fake()
    d10._pushed_to_strip = True
    d10._power_known = False
    d10.state.effect = int(P.Effect.fade_all)
    d10.state.effect_speed = 80
    d10.written = []

    async def collect3(payload):
        d10.written.append(payload)
    d10._write = collect3
    await d10._replay_state()
    check("при активном эффекте цвет не отправляется",
          P.color(*d10.state.color, d10.variant) not in d10.written,
          [p.hex() for p in d10.written])
    check("режим отправляется", P.effect(int(P.Effect.fade_all), d10.variant) in d10.written)
    check("скорость эффекта отправляется",
          P.effect_speed(80, d10.variant) in d10.written,
          "effect_speed терялся — лента откатывалась на дефолт")

    print("\n── регресс: счётчик сбрасывается при разрыве ──")
    # единственное присваивание 0 было в __init__: любой не-BleakError
    # оставлял счётчик выше нуля навсегда и сторож возвращался к рвению
    d11 = Fake()
    d11._inflight = 3
    d11._client = FakeClient()
    await d11._teardown()
    check("_teardown обнуляет счётчик", d11._inflight == 0, f"_inflight={d11._inflight}")

    print("\n── проба связи на простое ──")
    # сторож не должен молчать при простое: иначе зависший адаптер неделями
    # выглядит живым, ровно то против чего написана docstring _watchdog
    d12 = Fake()
    d12._client = FakeClient()
    d12.connected = True
    d12._ready.set()
    d12._queue = asyncio.Queue()
    d12._inflight = 0
    d12._last_write = time.monotonic() - 9999
    probes = {"n": 0}

    async def probe_write(payload):
        probes["n"] += 1
    d12._write = probe_write
    real_probe = D.Driver._probe
    D.WATCHDOG_PROBE = 0.05
    D.WATCHDOG_INTERVAL = 0.05
    d12._watch = asyncio.create_task(d12._watchdog())
    await asyncio.sleep(0.3)
    check("на простое сторож шлёт пробу", probes["n"] > 0, f"проб={probes['n']}")
    check("удачная проба не рвёт соединение",
          d12._client is not None and d12.connected, f"client={d12._client}")

    # проба зависла — соединение обязано упасть
    d13 = Fake()
    d13._client = FakeClient()
    d13.connected = True
    d13._ready.set()
    d13._queue = asyncio.Queue()
    d13._inflight = 0
    d13._last_write = time.monotonic() - 9999

    async def hung_write(payload):
        raise D.BleakError("запись в ленту зависла")
    d13._write = hung_write
    d13._watch = asyncio.create_task(d13._watchdog())
    await asyncio.sleep(0.3)
    check("зависшая проба рвёт соединение", d13._client is None, f"client={d13._client}")
    check("connected сброшен после зависшей пробы", d13.connected is False)
    d13._stop.set()
    d13._watch.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await d13._watch
    importlib.reload(D)

    print(f"\nитог: {ok} ок, {len(bad)} провалов")
    if bad:
        print("провалились: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
