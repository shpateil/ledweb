"""протокол elk-bledom: байт-посылки, эффекты, расписание.

формат кадра (9 байт):
    0x7E  LEN  CMD  P1 P2 P3 P4 P5  0xEF

len — маркер длины, у разных прошивок разный (0x00 / 0x04 / 0x07).
команды собраны из models.json (ha-интеграция dave-code-ruiz/elkbledom),
proTOCOL.md (FergusInLondon/ELK-BLEDOM) и lotuslantern (wl.smartled).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"
READ_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"

# дефолт для устройств без явной модели
DEFAULT_MAC = "AA:BB:CC:DD:EE:FF"


class Cmd(IntEnum):
    brightness = 0x01
    speed = 0x02
    effect = 0x03
    power = 0x04
    color = 0x05
    mic_level = 0x06
    mic_onoff = 0x07
    countdown = 0x76
    rgb_order = 0x81
    schedule = 0x82
    set_time = 0x83
    query_time = 0x85


class WeekDay(IntEnum):
    mon = 0x01
    tue = 0x02
    wed = 0x04
    thu = 0x08
    fri = 0x10
    sat = 0x20
    sun = 0x40

    @property
    def mask(self) -> int:
        return int(self)

    @classmethod
    def from_date(cls, d: datetime) -> "WeekDay":
        return list(cls)[d.weekday()]


WEEKDAYS_ALL = 0x7F
WEEKDAYS_WORK = 0x1F
WEEKDAYS_WEEKEND = 0x60


class Effect(IntEnum):
    """визуальные режимы. 0x80-0x87 — микрофон."""

    none = 0x00

    jump_rgb = 0x87
    jump_all = 0x88
    fade_rgb = 0x89
    fade_all = 0x8A
    crossfade_red = 0x8B
    crossfade_green = 0x8C
    crossfade_blue = 0x8D
    crossfade_yellow = 0x8E
    crossfade_cyan = 0x8F
    crossfade_magenta = 0x90
    crossfade_white = 0x91
    crossfade_red_green = 0x92
    crossfade_red_blue = 0x93
    crossfade_green_blue = 0x94
    blink_all = 0x95
    blink_red = 0x96
    blink_green = 0x97
    blink_blue = 0x98
    blink_yellow = 0x99
    blink_cyan = 0x9A
    blink_magenta = 0x9B
    blink_white = 0x9C

    # микрофон
    mic_erobic = 0x80
    mic_rhythm = 0x81
    mic_spectrum = 0x82
    mic_rolling = 0x83


EFFECT_NAMES_RU: dict[int, str] = {
    Effect.none: "статичный цвет",
    Effect.jump_rgb: "прыжок rgb",
    Effect.jump_all: "прыжок всех цветов",
    Effect.fade_rgb: "перелив rgb",
    Effect.fade_all: "перелив всех цветов",
    Effect.crossfade_red: "перелив красный",
    Effect.crossfade_green: "перелив зелёный",
    Effect.crossfade_blue: "перелив синий",
    Effect.crossfade_yellow: "перелив жёлтый",
    Effect.crossfade_cyan: "перелив голубой",
    Effect.crossfade_magenta: "перелив пурпурный",
    Effect.crossfade_white: "перелив белый",
    Effect.crossfade_red_green: "перелив красно-зелёный",
    Effect.crossfade_red_blue: "перелив красно-синий",
    Effect.crossfade_green_blue: "перелив зелёно-синий",
    Effect.blink_all: "мигание всех цветов",
    Effect.blink_red: "мигание красный",
    Effect.blink_green: "мигание зелёный",
    Effect.blink_blue: "мигание синий",
    Effect.blink_yellow: "мигание жёлтый",
    Effect.blink_cyan: "мигание голубой",
    Effect.blink_magenta: "мигание пурпурный",
    Effect.blink_white: "мигание белый",
    Effect.mic_erobic: "микрофон: эрбик",
    Effect.mic_rhythm: "микрофон: ритм",
    Effect.mic_spectrum: "микрофон: спектр",
    Effect.mic_rolling: "микрофон: волна",
}

# эффекты, которые не трогают заданный цвет (тот, который лента помнит)
COLORLESS_EFFECTS = {int(Effect.mic_erobic), int(Effect.mic_rhythm),
                      int(Effect.mic_spectrum), int(Effect.mic_rolling)}


@dataclass(frozen=True)
class Variant:
    """набор раскладок байт под конкретную прошивку.

    len_marker — что писать во втором байте.
    power_on/off — готовые шаблоны power-команды (P1 = 0xF0 вкл, 0x00 выкл).
    """

    name: str
    len_marker: int = 0x00
    power_on: bytes = bytes([0x7E, 0x00, 0x04, 0xF0, 0x00, 0x01, 0xFF, 0x00, 0xEF])
    power_off: bytes = bytes([0x7E, 0x00, 0x04, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF])
    color_tail: int = 0x00          # P4 в команде цвета
    color_temp_tail: int = 0x00
    brightness_mode: int = 0xFF    # P2 в команде яркости
    effect_speed_tail: tuple[int, int, int] = (0x00, 0x00, 0x00)
    effect_tail: tuple[int, int, int] = (0x03, 0xFF, 0xFF)


# наборы из моделей elkbledom/models.json, проверенные в живых репо
VARIANTS: dict[str, Variant] = {
    # самая частая прошивка elk-bledom: len=0x00, цвет с хвостом 0x00
    "generic": Variant("generic"),
    # вариант с len=0x07 и хвостом цвета 0x0a (есть в models.json у elk-bledom/bledob)
    "07": Variant(
        "07",
        len_marker=0x07,
        power_on=bytes([0x7E, 0x07, 0x04, 0xFF, 0x00, 0x01, 0x02, 0x01, 0xEF]),
        power_off=bytes([0x7E, 0x07, 0x04, 0x00, 0x00, 0x00, 0x02, 0x01, 0xEF]),
        color_tail=0x0A,
        effect_speed_tail=(0x00, 0x00, 0x00),
    ),
    # len=0x04 (модель elk-bleddm и др.)
    "04": Variant(
        "04",
        len_marker=0x04,
        power_on=bytes([0x7E, 0x04, 0x04, 0xF0, 0x00, 0x01, 0xFF, 0x00, 0xEF]),
        power_off=bytes([0x7E, 0x04, 0x04, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF]),
        color_tail=0x00,
        brightness_mode=0x00,
        effect_tail=(0x03, 0x00, 0x00),
    ),
}

DEFAULT_VARIANT = "generic"


def _u(v: int) -> int:
    return max(0, min(255, int(v)))


def hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """пересчёт hsv→rgb. фронт шлёт hsv, протокол ждёт rgb."""
    h = float(h) % 360
    s = max(0.0, min(1.0, float(s)))
    v = max(0.0, min(1.0, float(v)))
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    if h < 60:
        t = (c, x, 0.0)
    elif h < 120:
        t = (x, c, 0.0)
    elif h < 180:
        t = (0.0, c, x)
    elif h < 240:
        t = (0.0, x, c)
    elif h < 300:
        t = (x, 0.0, c)
    else:
        t = (c, 0.0, x)
    return tuple(int(round((u + m) * 255)) for u in t)


def power(on: bool, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    return v.power_on if on else v.power_off


def color(r: int, g: int, b: int, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    return bytes([0x7E, v.len_marker, 0x05, 0x03, _u(r), _u(g), _u(b), v.color_tail, 0xEF])


def white(brightness: int = 100, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    """«белый» = 0x01-команда с интенсивностью; по сути режим белого канала."""
    return bytes([0x7E, v.len_marker, 0x01, _u(brightness), 0x00, 0x00, 0x00, 0x00, 0xEF])


def brightness(value: int, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    return bytes([0x7E, v.len_marker, 0x01, _u(value), v.brightness_mode, 0x00, 0x00, 0x00, 0xEF])


def single_color(index: int, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    """предустановленный цвет ленты (0x05 / 0x01)."""
    return bytes([0x7E, v.len_marker, 0x05, 0x01, _u(index), 0x00, 0x00, 0x00, 0xEF])


def color_temp(warm: int, cold: int, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    """цветовая температура: warm+cold = 100 (проценты тёплого/холодного)."""
    warm = max(0, min(100, int(warm)))
    cold = max(0, min(100 - warm, int(cold)))
    return bytes([0x7E, v.len_marker, 0x05, 0x02, warm, cold, 0x00, v.color_temp_tail, 0xEF])


def effect(value: int, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    a, b, c = v.effect_tail
    return bytes([0x7E, v.len_marker, 0x03, _u(value), a, b, c, 0x00, 0xEF])


def effect_speed(value: int, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    a, b, c = v.effect_speed_tail
    return bytes([0x7E, v.len_marker, 0x02, _u(value), a, b, c, 0x00, 0xEF])


def mic_on(enabled: bool, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    return bytes([0x7E, v.len_marker, 0x07, 1 if enabled else 0, 0x00, 0x00, 0x00, 0x00, 0xEF])


def mic_level(value: int, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    return bytes([0x7E, v.len_marker, 0x06, _u(value), 0x00, 0x00, 0x00, 0x00, 0xEF])


def rgb_order(r: int, g: int, b: int, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    """порядок пинов. дефолт 0x010203 = rgb."""
    return bytes([0x7E, 0x06, 0x81, _u(r), _u(g), _u(b), 0xFF, 0x00, 0xEF])


def _timestamp(hour: int, minutes: int, days: int) -> bytes:
    """лента ждёт 3 байта: часы | минуты<<8 | дни-недели<<16."""
    t = ((hour & 0xFF) | ((minutes & 0xFF) << 8) | ((days & 0xFF) << 16))
    return bytes([t & 0xFF, (t >> 8) & 0xFF, (t >> 16) & 0xFF])


def schedule(on: bool, hour: int, minutes: int, days: int = WEEKDAYS_ALL,
             enabled: bool = True, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    """вкл/выкл по расписанию. дни — битовая маска WeekDay, +0x80 если активно."""
    ts = _timestamp(hour, minutes, days)
    flag = (days & 0xFF) | (0x80 if enabled else 0x00)
    return bytes([0x7E, 0x08, 0x82, ts[0], ts[1], ts[2], 1 if on else 0, flag, 0xEF])


def schedule_clear(on: bool, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    return bytes([0x7E, 0x08, 0x82, 0x00, 0x00, 0x00, 1 if on else 0, 0x00, 0xEF])


def set_time(now: datetime | None = None, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    now = now or datetime.now()
    ts = _timestamp(now.hour, now.minute, 1 << now.weekday())
    return bytes([0x7E, 0x07, 0x83, ts[0], ts[1], ts[2], 1 << now.weekday(), 0xFF, 0xEF])


def query_time(v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    """запрос расписания; ответ прилетает в notify по fff4."""
    return bytes([0x7E, 0x09, 0x85, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])


def countdown(minutes: int, v: Variant = VARIANTS[DEFAULT_VARIANT]) -> bytes:
    minutes = max(0, min(999, int(minutes)))
    return bytes([0x7E, 0x07, 0x76, minutes & 0xFF, (minutes >> 8) & 0xFF,
                  (minutes >> 16) & 0xFF, 0x00, 0xFF, 0xEF])


def reset() -> bytes:
    return bytes([0x7E, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0xEF])


# ── палитры и пресеты ────────────────────────────────────────────────

# материал-палитра (concrete-ish, читается на любом экране)
PALETTE_MATERIAL: list[tuple[str, int, int, int]] = [
    ("красный", 0xF44336, 0xFF5252, 0xFF8A80),
    ("розовый", 0xE91E63, 0xFF4081, 0xFF80AB),
    ("пурпурный", 0x9C27B0, 0xE040FB, 0xEA80FC),
    ("индиго", 0x3F51B5, 0x7986CB, 0x9FA8DA),
    ("синий", 0x2196F3, 0x42A5F5, 0x64B5F6),
    ("голубой", 0x03A9F4, 0x29B6F6, 0x4FC3F7),
    ("бирюзовый", 0x009688, 0x26A69A, 0x4DB6AC),
    ("зелёный", 0x4CAF50, 0x66BB6A, 0x81C784),
    ("лайм", 0x8BC34A, 0xAED581, 0xC5E1A5),
    ("жёлтый", 0xFFEB3B, 0xFFEE58, 0xFFF176),
    ("янтарь", 0xFFC107, 0xFFD54F, 0xFFE082),
    ("оранжевый", 0xFF5722, 0xFF8A65, 0xFFAB91),
    ("коричневый", 0x795548, 0xA1887F, 0xBCAAA4),
    ("серый", 0x9E9E9E, 0xBDBDBD, 0xE0E0E0),
]

# палитра для «настроения» — глубже и насыщеннее
PALETTE_MOOD: list[tuple[str, int, int, int]] = [
    ("закат", 0xFF512F, 0xF09819, 0xDD2476),
    ("закат2", 0xFC466B, 0x3F5EFB, 0xFC4A1A),
    ("космос", 0x360033, 0x0B8793, 0x4A00E0),
    ("север", 0x0F2027, 0x2C5364, 0x00C9FF),
    ("омбре", 0x8E2DE2, 0x4A00E0, 0xD16BA5),
    ("вишня", 0xEB3349, 0xF15C6D, 0x8E2DE2),
    ("неон", 0x00F5D4, 0x00BBF9, 0x9B5DE5),
    ("аврора", 0x00C9FF, 0x05D9E8, 0x00FFB7),
    ("море", 0x136A8A, 0x267871, 0x2CB5A0),
    ("жара", 0xF12711, 0xF5AF19, 0xFC4A1A),
    ("лаванда", 0x8E7CFF, 0xA18CD1, 0xFBC2EB),
    ("жасмин", 0xFC5C7D, 0x6A82FB, 0xC471ED),
    ("жара2", 0xFF512F, 0xDD2476, 0xAC1B6E),
    ("стальной", 0x485563, 0x29323C, 0x7A879A),
]

# температура света: (kelvin, warm%, cold%) — warm+cold=100
KELVIN_PRESETS: list[tuple[str, int, int, int]] = [
    ("свеча", 1800, 90, 10),
    ("лампа накала", 2700, 75, 25),
    ("тёплый", 3000, 65, 35),
    ("нейтральный", 4000, 45, 55),
    ("день", 5000, 30, 70),
    ("холодный", 6000, 20, 80),
    ("дневной свет", 7000, 10, 90),
]


def kelvin_to_warm_cold(kelvin: int) -> tuple[int, int]:
    """грубая, но достаточная шкала 1800K..7000K -> (warm%, cold%)."""
    k = max(1800, min(7000, int(kelvin)))
    warm = int(round(100 - (k - 1800) / (7000 - 1800) * 100))
    warm = max(0, min(100, warm))
    return warm, 100 - warm
