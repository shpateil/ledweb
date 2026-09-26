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
    kelvin: int | None = None
    mic: bool = False
    mic_level: int = 50
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
        self._last_write = 0.0
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._notify: list[bytes] = []
        self._ready = asyncio.Event()      # соединение живое, вода по очереди идёт

    # ── жизненный цикл ──────────────────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="led-driver")

    async def stop(self) -> None:
        self._stop.set()
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
                await self._open()
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
                await client.connect()
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
                    with contextlib.suppress(Exception):
                        await client.disconnect()
                    continue
                raise
            except Exception:
                with contextlib.suppress(Exception):
                    await client.disconnect()
                raise
            self._client = client
            self.connected = True
            self.last_error = None
            self._ready.set()
            if self.dev.sync_time:
                await self._write(P.set_time())
            with contextlib.suppress(Exception):
                await client.start_notify(P.READ_UUID, self._on_notify)
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
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=IDLE_POLL)
            except asyncio.TimeoutError:
                continue
            batch: list[Any] = list(first)
            while not self._queue.empty():
                batch.extend(self._queue.get_nowait())
            for item in batch:
                fut, payload = item if isinstance(item, tuple) else (None, item)
                try:
                    await self._write(payload)
                except BleakError as e:
                    if fut is not None and not fut.done():
                        fut.set_exception(e)
                    self.connected = False
                    self._ready.clear()
                    raise
                if fut is not None and not fut.done():
                    fut.set_result(True)

    async def _teardown(self) -> None:
        self.connected = False
        self._ready.clear()
        client, self._client = self._client, None
        if client is not None:
            # disconnect() на уже мёртвом клиенте может висеть вечно —
            # ограничиваем, иначе teardown не отдаст управление и демон встанет
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(client.disconnect(), timeout=5.0)
        # будим всех, кто ждал: они повторят после реконнекта
        while not self._queue.empty():
            item = self._queue.get_nowait()
            for sub in (item if isinstance(item, list) else [item]):
                if isinstance(sub, tuple) and sub[0] is not None and not sub[0].done():
                    sub[0].set_exception(BleakError("связь с лентой потеряна, переподключаюсь"))
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
        await client.write_gatt_char(P.WRITE_UUID, payload, response=False)
        self._last_write = time.monotonic()
        self.last_command_ms = (time.perf_counter() - t0) * 1000

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
            self._emit()
        await self._await_ready()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        batch: list[Any] = [(fut, p) for p in payloads]
        batch.append(fut)
        self._queue.put_nowait(batch)
        await asyncio.wait_for(fut, timeout=WRITE_TIMEOUT)

    async def set_power(self, on: bool) -> None:
        await self._send([P.power(on, self.variant)], {"power": on})

    async def set_color(self, r: int, g: int, b: int) -> None:
        v = self.variant
        await self._send(
            [P.single_color(0, v), P.color(r, g, b, v)],
            {"color": (r, g, b), "effect": int(P.Effect.none), "kelvin": None},
        )

    async def set_brightness(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        await self._send([P.brightness(value, self.variant)], {"brightness": value})

    async def set_effect(self, effect: int) -> None:
        await self._send([P.effect(effect, self.variant)], {"effect": int(effect)})

    async def set_effect_speed(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        await self._send([P.effect_speed(value, self.variant)], {"effect_speed": value})

    async def set_kelvin(self, kelvin: int) -> None:
        v = self.variant
        warm, cold = P.kelvin_to_warm_cold(kelvin)
        await self._send(
            [P.color(255, 255, 255, v), P.color_temp(warm, cold, v)],
            {"kelvin": int(kelvin), "effect": int(P.Effect.none)},
        )

    async def set_mic(self, enabled: bool) -> None:
        v = self.variant
        payloads = [P.mic_on(enabled, v)]
        patch: dict[str, Any] = {"mic": enabled}
        if enabled:
            effect = int(P.Effect.mic_spectrum)
            payloads += [P.mic_level(self.state.mic_level, v), P.effect(effect, v)]
            patch["effect"] = effect
        await self._send(payloads, patch)

    async def set_mic_level(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        await self._send([P.mic_level(value, self.variant)], {"mic_level": value})

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
        """единая точка входа из api. первый позиционный аргумент — команда."""
        action = _action
        kw.pop("action", None)
        if action == "color" and "r" not in kw:
            r, g, b = hsv_to_rgb(kw.get("h", 0), kw.get("s", 1.0), kw.get("v", 1.0))
            kw.update(r=r, g=g, b=b)
        table: dict[str, Callable[[], Any]] = {
            "power": lambda: self.set_power(bool(kw["on"])),
            "color": lambda: self.set_color(kw["r"], kw["g"], kw["b"]),
            "brightness": lambda: self.set_brightness(kw["value"]),
            "effect": lambda: self.set_effect(kw["value"]),
            "speed": lambda: self.set_effect_speed(kw["value"]),
            "kelvin": lambda: self.set_kelvin(kw["value"]),
            "mic": lambda: self.set_mic(bool(kw["on"])),
            "mic_level": lambda: self.set_mic_level(kw["value"]),
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
