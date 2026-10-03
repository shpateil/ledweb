"""персистентный ble-драйвер: одно соединение, одна очередь, кэш состояния.

правила, зафиксированные здесь:
  * соединение принадлежит ОДНОМУ владельцу — фоновой задаче `_run`.
    ни одна команда не дёргает connect/disconnect сама, только ждёт готовности.
  * весь трафик в ленту идёт через `_serve`, там же автопереподключение.
  * состояние обновляется оптимистично (до отправки) — интерфейс не ждёт ble.
  * bluez оставляет соединение в InProgress после обрыва: лечим disconnect+пауза,
    но только в фоновом потоке, никогда из горячего пути команды.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from . import protocol as P
from .protocol import hsv_to_rgb

log = logging.getLogger("ledweb.driver")

MIN_GAP = 0.025         # минимальный зазор между командами, сек
CONNECT_TIMEOUT = 10.0
WRITE_TIMEOUT = 8.0     # сколько ждём выполнения команды в очереди
GATT_WRITE_TIMEOUT = 2.0  # сколько ждём саму запись в ленту. dbus-вызов без
                          # ответа может висеть вечно на зависшем bluez, и если
                          # не ограничить — writer встанет колом навсегда
WATCHDOG_INTERVAL = 20.0   # как часто сторож проверяет связь
WATCHDOG_PROBE = 900.0     # после столького простоя шлём пробную запись:
                         # молчание bluez неотличимо от «всё хорошо», и без
                         # пробы зависший адаптер неделями выглядит живым
WATCHDOG_IDLE = 120.0      # больше не используется, оставлен как документ
                         # прежней логики. не опираться на него в коде
WATCHDOG_STUCK = 30.0      # очередь не двигается дольше этого — связь мертва
OPEN_TIMEOUT = 45.0     # весь _open() целиком, включая 4 попытки и кики
DROP_TIMEOUT = 3.0       # disconnect в тот же dbus и тоже умеет не вернуться
NOTIFY_TIMEOUT = 3.0     # подписка на fff4. лента её не поддерживает и иногда
                         # не возвращает ответ вовсе, а без таймаута весь
                         # цикл подключения зависал навсегда
READY_TIMEOUT = 14.0    # сколько ждём, пока фоновый поток поднимет соединение
RECONNECT_BACKOFF = (0.4, 0.8, 1.6, 3.0, 5.0, 8.0)
IDLE_POLL = 0.5         # как часто проверять, что соединение ещё живо


@dataclass
class State:
    power: bool = False
    color: tuple[int, int, int] = (255, 255, 255)
    brightness: int = 100
    effect: int = int(P.Effect.none)
    effect_speed: int = 50
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        r, g, b = d.pop("color")
        d["color"] = {"r": r, "g": g, "b": b, "hex": f"#{r:02x}{g:02x}{b:02x}"}
        d["effect_name"] = P.EFFECT_NAMES_RU.get(d["effect"], "—")
        return d


@dataclass
class Device:
    mac: str = P.DEFAULT_MAC
    name: str = "ELK-BLEDOM"
    variant: str = P.DEFAULT_VARIANT
    auto_connect: bool = True
    sync_time: bool = True


def _parse_color(kw: dict[str, Any]) -> tuple[int, int, int]:
    """достаёт rgb из запроса. раньше тут был только hsv, и запрос с одним
    hex молча давал чистый красный: h=0, s=1, v=1 по умолчанию. фронт шлёт
    сразу hex и rgb, поэтому там работало, а любой другой клиент получал
    не тот цвет и без единой ошибки.
    """
    if kw.get("rgb"):
        seq = list(kw["rgb"])
        if len(seq) == 3:
            return tuple(max(0, min(255, int(x))) for x in seq)   # type: ignore[return-value]
    hx = kw.get("hex")
    if hx:
        s = str(hx).lstrip("#")
        if len(s) == 3:                      # краткая запись #f0a
            s = "".join(c * 2 for c in s)
        if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        raise ValueError(f"не понял цвет {hx!r}, жду #rrggbb или #rgb")
    return hsv_to_rgb(kw.get("h", 0), kw.get("s", 1.0), kw.get("v", 1.0))


class Driver:
    def __init__(self, dev: Device, on_change: Callable[[State], None] | None = None):
        self.dev = dev
        self.state = State()
        self.state.updated_at = time.time()
        self.connected = False
        self.last_error: str | None = None
        self.last_command_ms: float | None = None
        self.on_change = on_change
        self.variant: P.Variant = P.VARIANTS[dev.variant]

        self._client: BleakClient | None = None
        self._last_write = time.monotonic()   # с этого момента и считаем простой
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._watch: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._pending: dict[asyncio.Future, int] = {}   # future → сколько кадров осталось
        self._notify: list[bytes] = []
        self._ready = asyncio.Event()      # соединение живое, вода по очереди идёт
        # состояние хотя бы раз отправлялось ленте. до этого демон держит
        # дефолт State() и перезаписывать ленту на старте нельзя: пользователь
        # мог держать её на своём режиме со вчерашнего рана
        self._pushed_to_strip = False
        # питание юзер трогал явно. до этого power в кэше — просто дефолт
        # False, и реконнект гасил ленту, которую никто не выключал
        self._power_known = False
        # сколько записей сейчас в полёте. очередь пуста ≠ записи не идут:
        # _serve снимает кадр из очереди ДО записи, поэтому просто «пустая
        # очередь» как признак простоя врёт. сторож смотрит на сумму.
        self._inflight = 0

    # ── жизненный цикл ──────────────────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="led-driver")
        self._watch = asyncio.create_task(self._watchdog(), name="led-watchdog")

    async def _watchdog(self) -> None:
        """сторож: следит, что связь реально живая, а не только помечена как живая.

        без него демон мог простоять сутки с `connected: true`, не сумев ни
        одной записи: состояние врало, панель показывала «всё ок», а лента не
        реагировала. если очередь не двигалась дольше порога — рвём соединение,
        и фоновый цикл поднимает новое.
        """
        while not self._stop.is_set():
            await asyncio.sleep(WATCHDOG_INTERVAL)
            if self._stop.is_set():
                return
            if self._client is None or not self._client.is_connected:
                continue                      # этим занимается _run
            # зависание возможно только когда в очереди что-то лежит: если
            # очередь пуста, писать некому и зависнуть нечему. WATCHDOG_IDLE
            # тут больше не участвует — раньше стояло «пустая очередь И простой
            # меньше WATCHDOG_IDLE», и при простое больше 120 с оба условия
            # проходили насквозь: следующий if проверял простой > 30 с, что
            # для простоя истинно всегда. демон рвал живое, идеально
            # простаивающее соединение каждые ~140 с (WATCHDOG_INTERVAL 20 +
            # порог 120), и лента на каждом обрыве уходила в свой дефолтный
            # режим с перебором цветов — 1496 раз за неделю в журнале
            if self._queue.empty() and self._inflight == 0:
                # работы нет. просто continue тут опасен: молчание bluez
                # неотличимо от «всё хорошо», client.is_connected остаётся
                # True неопределённо долго, демон показывает connected: true
                # и не делает ни одной попытки переподключения. ровно то,
                # против чего написана docstring этого метода
                if time.monotonic() - self._last_write < WATCHDOG_PROBE:
                    continue
                await self._probe()
                continue
            # команды висят в очереди дольше порога — писать не отвечает
            if time.monotonic() - self._last_write > WATCHDOG_STUCK:
                log.warning("завис %s с при непустой очереди, рву соединение", WATCHDOG_STUCK)
                self.connected = False
                self.last_error = "запись в ленту не отвечает"
                self._ready.clear()
                self._emit()      # без этого фронт не узнает об обрыве
                client, self._client = self._client, None
                if client is not None:
                    with contextlib.suppress(Exception, asyncio.TimeoutError):
                        await asyncio.wait_for(client.disconnect(), timeout=3.0)

    async def _probe(self) -> None:
        """дешёвая проверка связи на простое: одна запись и лог.

        шлём set_time — он только часы ленте ставит, ни цвет, ни режим, ни
        яркость не трогает, так что проба не видна. если и она зависла,
        рвём соединение, и фоновый цикл поднимет новое. _write сам ловит
        таймаут и рвёт клиент, нам остаётся привести состояние в порядок.
        """
        log.info("простой %.0f с, пробую связь записью", WATCHDOG_PROBE)
        self._inflight += 1
        try:
            await self._write(P.set_time())
            log.info("проба связи прошла")
        except Exception as e:                    # noqa: BLE001
            log.warning("проба связи не прошла (%s), рву соединение", e)
            self.connected = False
            self.last_error = "лента не отвечает на запись"
            self._ready.clear()
            self._emit()
            client, self._client = self._client, None
            if client is not None:
                with contextlib.suppress(Exception, asyncio.TimeoutError):
                    await asyncio.wait_for(client.disconnect(), timeout=3.0)
        finally:
            self._inflight -= 1

    async def stop(self) -> None:
        self._stop.set()
        if self._watch:
            self._watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._teardown()

    async def _run(self) -> None:
        """единственный владелец соединения: поднял → обслужил очередь → упал → поднял."""
        backoff = list(RECONNECT_BACKOFF)
        while not self._stop.is_set():
            if not self.dev.auto_connect:
                await asyncio.sleep(1)
                continue
            try:
                # каждый шаг под таймаутом. любая из этих функций при зависшем
                # bluez умеет не вернуться, и тогда демон тихо стоит годами:
                # процесс жив, лента в кэше, а попыток подключения ноль
                await asyncio.wait_for(self._open(), timeout=OPEN_TIMEOUT)
                backoff = list(RECONNECT_BACKOFF)
                await self._serve()
            except asyncio.CancelledError:
                raise
            except Exception as e:            # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
                log.info("ble: %s", self.last_error)
                await self._teardown()
                await asyncio.sleep(backoff.pop(0) if len(backoff) > 1 else 8.0)
            else:
                # _serve() вернулся штатно = соединение отвалилось само. если
                # не вызвать teardown и не поспать, цикл превращается в горячий
                # спин: _open() видит не-None клиент и выходит, _serve() сразу
                # возвращается, паузы нет — демон ест 100% cpu
                await self._teardown()
                await asyncio.sleep(1.0)

    async def _open(self) -> None:
        """поднять соединение. только из фонового потока, никогда не параллельно."""
        if self._client is not None:
            return
        for attempt in range(4):
            if self._stop.is_set():
                return
            if attempt:
                await self._kick()
            log.info("подключаюсь к %s (попытка %d)", self.dev.mac, attempt + 1)
            client = BleakClient(self.dev.mac, timeout=CONNECT_TIMEOUT)
            try:
                await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
            except BleakDeviceNotFoundError:
                log.debug("устройства нет в кэше bluez, сканирую")
                await self._scan()
                continue
            except BleakDBusError as e:
                msg = str(e)
                if "InProgress" in msg or "In Progress" in msg:
                    log.debug("bluez занят предыдущей попыткой, сбрасываю")
                    # неудачный клиент нельзя оставлять в self._client: _open()
                    # считает что соединение уже есть и выходит, а следующая
                    # итерация снова падает. рвём его явно.
                    await self._drop(client)
                    continue
                raise
            except Exception:
                await self._drop(client)
                raise
            self._client = client
            self.connected = True
            self.last_error = None
            self._ready.set()
            self._last_write = time.monotonic()   # сброс счётчика простоя
            if self.dev.sync_time:
                await self._write(P.set_time())
            # после обрыва лента возвращается в свой дефолтный режим с
            # перебором цветов: состояние живёт в её памяти и наш обрыв ей
            # ничего не сообщает. раньше здесь шёл только set_time, и каждый
            # реконнект оставлял ленту мигать случайным цветом, пока
            # пользователь не дёрнет панель. шлём своё состояние обратно
            if await self._replay_state():
                log.info("состояние вернул ленте: %s", self._state_cmd())
            # start_notify на fff4 у этой ленты всегда валится с GATT Protocol
            # Error: Unlikely Error, а dbus-вызов при этом умеет не вернуться
            # вовсе. без таймаута _open() висел вечно: демон жив, лента
            # «подключена» в кэше, но ни одной попытки подпереподключения часами.
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(
                    client.start_notify(P.READ_UUID, self._on_notify),
                    timeout=NOTIFY_TIMEOUT,
                )
            log.info("подключено, mtu=%s", getattr(client, "mtu_size", "?"))
            self._emit()
            return
        raise BleakError("не удалось подключиться после 4 попыток")

    async def _serve(self) -> None:
        """качать очередь, пока соединение живо. вернётся, когда оно упало."""
        while not self._stop.is_set():
            if self._client is None or not self._client.is_connected:
                self.connected = False
                self._ready.clear()
                # клиент без is_connected — висящий. отдаём в _teardown,
                # иначе следующий _open() решит что соединение уже есть
                return
            # в очереди лежит кортеж (future, payload) на каждый кадр.
            # маркера нет: у команды один future на несколько payload, и
            # резолвится он только когда все её кадры записаны
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=IDLE_POLL)
            except asyncio.TimeoutError:
                continue
            items: list[tuple[asyncio.Future | None, bytes]] = [first]
            while not self._queue.empty():
                items.append(self._queue.get_nowait())
            for fut, payload in items:
                self._inflight += 1
                try:
                    await self._write(payload)
                except BleakError as e:
                    self._pending.pop(fut, None)
                    if fut is not None and not fut.done():
                        fut.set_exception(e)
                    self.connected = False
                    self._ready.clear()
                    return
                finally:
                    # обязательно в finally, а не двумя точками: любое
                    # исключение из _write (не только BleakError) иначе
                    # оставит счётчик выше нуля навсегда, сторож решит что
                    # запись зависла, и демон вернётся к рвению живого
                    # соединения каждые ~140 с — ровно тот баг, который тут
                    # и чинится
                    self._inflight -= 1
                if fut is None:
                    continue
                # резолвим future только когда записаны все кадры команды
                left = self._pending.get(fut)
                if left is not None:
                    left -= 1
                    if left <= 0:
                        self._pending.pop(fut, None)
                        if not fut.done():
                            fut.set_result(True)
                    else:
                        self._pending[fut] = left

    async def _teardown(self) -> None:
        self.connected = False
        self._ready.clear()
        # записи в полёте не переживают разрыв: счётчик обязан вернуться в
        # ноль, иначе сторож навсегда увидит «есть работа» и вернётся
        # к рвению простаивающего соединения — к исходному багу
        self._inflight = 0
        client, self._client = self._client, None
        if client is not None:
            # disconnect() на уже мёртвом клиенте может висеть вечно —
            # ограничиваем, иначе teardown не отдаст управление и демон встанет
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(client.disconnect(), timeout=5.0)
        # будим всех, кто ждал: они повторят после реконнекта
        self._pending.clear()
        while not self._queue.empty():
            item = self._queue.get_nowait()
            fut = item[0] if isinstance(item, tuple) and item else None
            if isinstance(fut, asyncio.Future) and not fut.done():
                fut.set_exception(BleakError("связь с лентой потеряна, переподключаюсь"))
        self._emit()

    async def _kick(self) -> None:
        """рвём висящее соединение, из-за которого bluez отдаёт InProgress.

        _kick зовут только когда self._client уже None, так что bluetoothctl
        не задевает наше живое соединение.
        """
        with contextlib.suppress(Exception, subprocess.TimeoutExpired):
            subprocess.run(["bluetoothctl", "disconnect", self.dev.mac],
                           capture_output=True, timeout=5)
        await asyncio.sleep(0.5)

    async def _scan(self) -> None:
        """возвращаем устройство в кэш bluez."""
        with contextlib.suppress(Exception):
            await BleakScanner.discover(timeout=3.0, return_adv=True)

    # ── запись ──────────────────────────────────────────────────────
    async def _write(self, payload: bytes) -> None:
        client = self._client
        if client is None or not client.is_connected:
            raise BleakError("нет соединения с лентой")
        gap = MIN_GAP - (time.monotonic() - self._last_write)
        if gap > 0:
            await asyncio.sleep(gap)
        t0 = time.perf_counter()
        # write_gatt_char(response=False) шлёт в dbus без ответа и может зависнуть
        # навсегда (зависший bluez, потерянный адаптер). без таймаута демон
        # вставал колом: writer ждал вечно, очередь не двигалась, и все
        # следующие команды тоже упирались в write timeout. ловим зависание
        # здесь и роняем соединение, чтобы _teardown поднял новое
        try:
            await asyncio.wait_for(
                client.write_gatt_char(P.WRITE_UUID, payload, response=False),
                timeout=GATT_WRITE_TIMEOUT,
            )
        except asyncio.TimeoutError as e:
            self.connected = False
            self._ready.clear()
            log.warning("запись в ленту зависла на %.1f с, рву соединение", WRITE_TIMEOUT)
            raise BleakError("запись в ленту зависла, переподключаюсь") from e
        self._last_write = time.monotonic()
        self.last_command_ms = (time.perf_counter() - t0) * 1000

    def _state_cmd(self) -> str:
        """человеческое имя текущего состояния — только для лога."""
        r, g, b = self.state.color
        return (f"power={self.state.power} color=#{r:02x}{g:02x}{b:02x} "
                f"brightness={self.state.brightness} effect={self.state.effect}")

    async def _replay_state(self) -> bool:
        """вернуть ленте то, что она забыла при обрыве.

        состояние живёт в памяти ленты, а не в демоне: после обрыва она
        остаётся в своём дефолтном режиме с перебором цветов. шлём питание,
        режим, яркость и цвет, чтобы лента выглядела как до обрыва. порядок
        тот же, что у обычных команд: режим и питание, потом яркость, потом
        цвет — иначе цвет гаснет на неверной яркости.

        пока состояние ни разу не уходило в ленту (свежий старт демона), молчим:
        наш State() тогда просто дефолт, и слать его — значит затереть режим,
        который юзер выставил вчера.
        """
        if not self._pushed_to_strip:
            return False
        # питание шлём только если юзер его явно трогал. State.power по
        # умолчанию False и никогда не читается с ленты (readback нет), так
        # что без этого флага реконнект гасил ленту, которую не выключали:
        # жмём только цвет → _pushed_to_strip взводится, power остаётся
        # дефолтным False → на обрыве уходит P.power_off
        if self._power_known:
            payloads = [P.power(bool(self.state.power), self.variant)]
        else:
            payloads = []
        effect = int(self.state.effect)
        # цвет идёт только при effect == none. set_color() сам гасит эффект
        # (кладёт effect=none в state_patch), значит «цвет значит эффект
        # выключен» — правило этого драйвера. если в кэше эффект не none,
        # слать цвет значит сбить его в статичный, и лента покажет одно,
        # а snapshot() — другое
        if effect == int(P.Effect.none):
            payloads += [P.single_color(0, self.variant),
                         P.color(*self.state.color, self.variant)]
        else:
            # скорость живёт в памяти ленты, как и режим, её тоже возвращаем
            payloads += [P.effect(effect, self.variant),
                         P.effect_speed(int(self.state.effect_speed), self.variant)]
        if not payloads:
            return False
        try:
            # весь отрезок считаем одной записью в полёте: пишем напрямую,
            # минуя очередь, иначе посреди серии обрыв не увидят ни
            # _teardown(), ни сторож
            self._inflight += 1
            for p in payloads:
                await self._write(p)
            return True
        except Exception as e:                    # noqa: BLE001
            log.warning("состояние вернуть не удалось: %s", e)
            # если при этом ещё и соединение развалилось — не даём _open()
            # рапортовать об успехе и уйти в _serve() с мёртвым клиентом:
            # тот там сразу падает и крутится в цикле. пусть _run увидит
            # исключение и поднимет соединение заново, состояние вернётся
            # на следующей попытке
            if self._client is None or not self._client.is_connected:
                raise BleakError("состояние вернуть не удалось, соединение мертво") from e
            # соединение живо, просто кадр не ушёл: лента покажет дефолт,
            # пользователь переключит сам. подключение не рвём
            return False
        finally:
            self._inflight -= 1

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._notify.append(bytes(data))
        del self._notify[:-32]
        log.debug("notify fff4: %s", bytes(data).hex())

    async def _await_ready(self) -> None:
        """ждём, пока фоновый поток поднимет соединение. сами не подключаемся."""
        if self._ready.is_set() and self._client is not None and self._client.is_connected:
            return
        self._ready.clear()
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=READY_TIMEOUT)
        except asyncio.TimeoutError:
            raise BleakError(f"лента не отвечает ({self.last_error or 'нет связи'})")

    async def _drop(self, client) -> None:
        """рвём клиент, не дав зависнуть.

        disconnect() идёт в тот же dbus, что и connect, и на зависшем bluez
        тоже умеет не вернуться. в try/except это не спасает: await висит
        вечно и тянет за собой весь цикл. поэтому таймаут обязателен.
        """
        if client is None:
            return
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(client.disconnect(), timeout=DROP_TIMEOUT)

    def _emit(self) -> None:
        if self.on_change:
            try:
                self.on_change(self.state)
            except Exception:                    # noqa: BLE001
                log.debug("on_change упал", exc_info=True)

    # ── команды ─────────────────────────────────────────────────────
    async def _send(self, payloads: list[bytes], state_patch: dict[str, Any] | None = None) -> None:
        """оптимистично обновляем кэш, кладём в очередь, ждём выполнения."""
        if state_patch:
            for k, val in state_patch.items():
                setattr(self.state, k, val)
            self.state.updated_at = time.time()
            # команда с меняющим состоянием — значит картинка в ленте теперь
            # наша, и её надо будет вернуть после следующего обрыва
            self._pushed_to_strip = True
            self._emit()
        await self._await_ready()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        # в очередь кладём по (future, payload) на каждый кадр. future один на
        # всю команду, но резолвить его можно только когда записаны все кадры —
        # иначе set_color ждал бы выхода по первому из двух кадров, а второй
        # ещё в полёте. раньше третьим элементом батча клался сам future, и
        # writer писал его в ленту как bytes(future) == b'' лишним пустым кадром
        self._pending[fut] = len(payloads)
        for p in payloads:
            self._queue.put_nowait((fut, p))
        await asyncio.wait_for(fut, timeout=WRITE_TIMEOUT)

    async def set_power(self, on: bool) -> None:
        self._power_known = True          # единственный источник правды о питании
        await self._send([P.power(on, self.variant)], {"power": on})

    async def set_color(self, r: int, g: int, b: int) -> None:
        v = self.variant
        await self._send(
            [P.single_color(0, v), P.color(r, g, b, v)],
            {"color": (r, g, b), "effect": int(P.Effect.none)},
        )

    async def set_brightness(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        await self._send([P.brightness(value, self.variant)], {"brightness": value})

    async def set_effect(self, effect: int) -> None:
        await self._send([P.effect(effect, self.variant)], {"effect": int(effect)})

    async def set_effect_speed(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        await self._send([P.effect_speed(value, self.variant)], {"effect_speed": value})

    async def schedule(self, on: bool, hour: int, minute: int, days: int) -> None:
        await self._send([P.schedule(on, hour, minute, days, True, self.variant)])

    async def schedule_clear(self, on: bool) -> None:
        await self._send([P.schedule_clear(on, self.variant)])

    async def query_schedule(self) -> list[bytes]:
        await self._send([P.query_time(self.variant)])
        await asyncio.sleep(0.5)
        return list(self._notify)

    async def set_countdown(self, minutes: int) -> None:
        await self._send([P.countdown(minutes, self.variant)])

    async def sync_time(self) -> None:
        await self._send([P.set_time()])

    async def scan(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        items = await BleakScanner.discover(timeout=timeout, return_adv=True)
        for item in items:
            dev = item[0] if isinstance(item, tuple) else item
            name = (getattr(dev, "name", "") or "")
            if any(k in name.upper() for k in ("ELK", "BLEDOM", "MELK", "STRIP")):
                found.append({
                    "address": getattr(dev, "address", "?"),
                    "name": name,
                    "rssi": getattr(item[1], "rssi", None) if isinstance(item, tuple) else None,
                })
        return found

    async def apply(self, _action: str, **kw: Any) -> dict[str, Any]:
        """униная точка входа из api. первый позиционный аргумент — команда."""
        action = _action
        kw.pop("action", None)
        if action == "color" and "r" not in kw:
            cr, cg, cb = _parse_color(kw)
            kw.update(r=cr, g=cg, b=cb)
        table: dict[str, Callable[[], Any]] = {
            "power": lambda: self.set_power(bool(kw["on"])),
            "color": lambda: self.set_color(kw["r"], kw["g"], kw["b"]),
            "brightness": lambda: self.set_brightness(kw["value"]),
            "effect": lambda: self.set_effect(kw["value"]),
            "speed": lambda: self.set_effect_speed(kw["value"]),
            "schedule": lambda: self.schedule(bool(kw["on"]), int(kw["hour"]),
                                              int(kw["minute"]), int(kw.get("days", 0x7F))),
            "schedule_clear": lambda: self.schedule_clear(bool(kw["on"])),
            "countdown": lambda: self.set_countdown(int(kw["minutes"])),
            "sync_time": self.sync_time,
        }
        fn = table.get(action)
        if fn is None:
            raise ValueError(f"неизвестное действие: {action}")
        await fn()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        d = self.state.to_dict()
        d.update({
            "mac": self.dev.mac,
            "name": self.dev.name,
            "connected": self.connected,
            "last_error": self.last_error,
            "last_command_ms": round(self.last_command_ms, 1) if self.last_command_ms else None,
            "variant": self.dev.variant,
            "notify": [b.hex() for b in self._notify[-8:]],
        })
        return d
