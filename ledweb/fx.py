"""софтверные эффекты: то, чего нет в прошивке ленты.

железо умеет только сплошной цвет и 7 базовых анимаций. всё остальное —
радугу, дыхание, огонь, бегущие полосы, пульс — считаем здесь и шлём
демону обычные команды цвета. цикл живёт в демоне, потому что лента
одна на весь дом и у неё свой ble-клиент.

инвариант: движок не трогает ble напрямую, только зовёт колбэк с
следующим цветом. так его можно тестировать без железа вообще.
"""

from __future__ import annotations

import asyncio
import colorsys
import contextlib
import logging
import math
import time
import traceback
from typing import Any, Awaitable, Callable

log = logging.getLogger("ledweb.fx")

ColorFn = Callable[[float], tuple[int, int, int]]


def hsv(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 360.0 / 360.0, max(0.0, min(1.0, s)),
                                   max(0.0, min(1.0, v)))
    return (int(r * 255), int(g * 255), int(b * 255))


# ── палитры эффектов ──────────────────────────────────────────────────
# каждый эффект это функция времени в секундах -> цвет. t растёт бесконечно.

def fx_static(t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    return base


def fx_rainbow(t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    h, s, v = colorsys.rgb_to_hsv(*[c / 255 for c in base])
    return hsv(t * 60.0, max(0.7, s), v)


def fx_breath(t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    h, s, v = colorsys.rgb_to_hsv(*[c / 255 for c in base])
    k = (math.sin(t * 1.4) + 1) / 2.0
    return hsv(h * 360.0, s, v * (0.12 + 0.88 * k))


def fx_pulse(t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    """резкие удары сердца с затуханием — читается издалека."""
    h, s, v = colorsys.rgb_to_hsv(*[c / 255 for c in base])
    period = (t % 1.6)
    if period < 0.14:
        k = 1.0
    elif period < 0.30:
        k = 1.0 - (period - 0.14) / 0.16
    else:
        k = 0.0
    k = 0.18 + 0.82 * (k ** 2)
    return hsv(h * 360.0, s, v * k)


def fx_fire(t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    """огонь: красно-оранжевая палитра с шумом по яркости."""
    flick = (math.sin(t * 11.0) * 0.5 + math.sin(t * 27.3) * 0.3
             + math.sin(t * 3.1) * 0.2)
    k = 0.62 + 0.38 * (flick + 1) / 2.0
    return (int(255 * k), int((120 + 70 * k) * k), int(28 * k))


def fx_stripes(t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    """две полосы, ползущие навстречу. лента не адресуемая, поэтому полоса
    имитируется миганием всего цвета — честно это называем stripes."""
    phase = (t * 0.9) % 2.0
    return base if phase < 1.0 else tuple(255 - c for c in base)  # type: ignore[return-value]


def fx_sunset(t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    cycle = (t % 30.0) / 30.0
    if cycle < 0.5:
        h = 340.0 - cycle * 2 * 170.0
        return hsv(h, 0.85, 0.95)
    h = 10.0 + (cycle - 0.5) * 2 * 60.0
    return hsv(h, 0.5, 0.35 + (cycle - 0.5) * 1.0)


def fx_chase(t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    h, s, v = colorsys.rgb_to_hsv(*[c / 255 for c in base])
    k = int(t * 6) % 3
    boost = (1.0, 0.55, 0.25)[k]
    return hsv(h * 360.0, s, v * boost)


def fx_aurora(t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    a = math.sin(t * 0.7)
    b = math.sin(t * 0.43 + 1.7)
    h1 = (150.0 + 60.0 * a) % 360.0
    h2 = (280.0 + 50.0 * b) % 360.0
    mix = (math.sin(t * 0.31) + 1) / 2.0
    r1, g1, b1 = hsv(h1, 0.7, 0.85)
    r2, g2, b2 = hsv(h2, 0.6, 0.6)
    return (int(r1 * (1 - mix) + r2 * mix),
            int(g1 * (1 - mix) + g2 * mix),
            int(b1 * (1 - mix) + b2 * mix))


def fx_twinkle(t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    k = (math.sin(t * 3.3) + math.sin(t * 1.9 + 0.8) + 1) / 2.0
    return tuple(int(c * (0.35 + 0.65 * k)) for c in base)  # type: ignore[return-value]


def fx_beat(t: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    """бонус-пульс, который ещё точнее pulse: двойной удар как у метронома."""
    period = t % 1.0
    if period < 0.08:
        k = 1.0
    elif period < 0.20:
        k = 0.4
    else:
        k = 0.10 + 0.14 * max(0.0, math.sin(t * 6.28))
    return tuple(int(c * k) for c in base)  # type: ignore[return-value]


EFFECTS: dict[str, dict[str, Any]] = {
    "static":   {"fn": fx_static,   "name": "статичный цвет",   "fps": 0,   "built_in": None},
    "rainbow":  {"fn": fx_rainbow,  "name": "радуга",           "fps": 20,  "built_in": None},
    "breath":   {"fn": fx_breath,   "name": "дыхание",         "fps": 12,  "built_in": None},
    "pulse":    {"fn": fx_pulse,    "name": "пульс",           "fps": 24,  "built_in": None},
    "beat":     {"fn": fx_beat,     "name": "двойной удар",    "fps": 24,  "built_in": None},
    "fire":     {"fn": fx_fire,     "name": "огонь",           "fps": 18,  "built_in": None},
    "stripes":  {"fn": fx_stripes,  "name": "полосы",          "fps": 8,   "built_in": None},
    "sunset":   {"fn": fx_sunset,   "name": "закат",           "fps": 10,  "built_in": None},
    "chase":    {"fn": fx_chase,    "name": "погоня",          "fps": 14,  "built_in": None},
    "aurora":   {"fn": fx_aurora,   "name": "северное сияние", "fps": 16,  "built_in": None},
    "twinkle":  {"fn": fx_twinkle,  "name": "мерцание",        "fps": 16,  "built_in": None},
}

# порядок для интерфейса
ORDER = list(EFFECTS)


class Engine:
    """крутит софтверный эффект и отдаёт цвет через колбэк.

    один экземпляр на демон. сам пишет в очередь, но ble не трогает.
    """

    def __init__(self, send: Callable[[tuple[int, int, int]], Awaitable[Any]],
                 on_tick: Callable[[], None] | None = None) -> None:
        self._send = send
        self._on_tick = on_tick
        self._task: asyncio.Task | None = None
        self.name: str = "static"
        self.speed: float = 1.0
        self.base: tuple[int, int, int] = (255, 255, 255)
        self._t0 = 0.0
        self._last: tuple[int, int, int] | None = None
        self.frames = 0
        self.errors = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, name: str, base: tuple[int, int, int], speed: float = 1.0) -> bool:
        if name not in EFFECTS:
            return False
        self.stop()
        self.name = name
        self.base = base
        self.speed = max(0.1, min(4.0, float(speed)))
        self._t0 = time.monotonic()
        self._last = None
        if EFFECTS[name]["fps"] <= 0:
            return False
        self._task = asyncio.create_task(self._loop(), name="fx")
        log.info("софт-эффект: %s, %.1fx", name, self.speed)
        return True

    def retint(self, base: tuple[int, int, int]) -> None:
        """смена цвета на ходу, не перезапуская эффект."""
        self.base = base

    def stop(self) -> None:
        t, self._task = self._task, None
        if t is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                log.debug("софт-эффект %s остановлен, стопнул: %s", self.name,
                          " > ".join(f"{f.name}:{f.lineno}" for f in
                                     traceback.extract_stack()[:-1][-4:]))

    async def _loop(self) -> None:
        fps = EFFECTS[self.name]["fps"]
        fn = EFFECTS[self.name]["fn"]
        interval = 1.0 / max(1, fps)
        try:
            while True:
                now = time.monotonic()
                t = (now - self._t0) * self.speed
                color = fn(t, self.base)
                # не шлём повторы: лента получит тот же байт дважды
                if color != self._last:
                    self._last = color
                    try:
                        await self._send(color)
                        self.frames += 1
                    except Exception as e:       # noqa: BLE001
                        # потеря связи не должна убивать эффект: считаем и ждём
                        # следующего кадра, реконнектом занимается драйвер
                        self.errors += 1
                        if self.errors in (1, 20) or self.errors % 100 == 0:
                            log.warning("кадр %d не ушёл: %s", self.errors, e)
                    if self._on_tick:
                        self._on_tick()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:                    # noqa: BLE001
            log.exception("цикл эффекта упал")

    def state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": EFFECTS[self.name]["name"],
            "running": self.running,
            "speed": self.speed,
            "fps": EFFECTS[self.name]["fps"],
            "frames": self.frames,
            "errors": self.errors,
        }
