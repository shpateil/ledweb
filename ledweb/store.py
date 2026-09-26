"""состояние, пресеты, сцены, расписание. чистый python, без внешних зависимостей."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

DATA_DIR = os.path.expanduser("%h/.config/ledweb")
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")
RULES_FILE = os.path.join(DATA_DIR, "rules.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# сцены — готовые наборы состояний, применяются одним кликом
SCENES: list[dict[str, Any]] = [
    {"id": "wake", "name": "подъём", "icon": "sunrise", "color": (255, 190, 120), "brightness": 60, "effect": 0},
    {"id": "work", "name": "работа", "icon": "laptop", "color": (255, 255, 255), "brightness": 100, "effect": 0},
    {"id": "evening", "name": "вечер", "icon": "lamp", "color": (255, 150, 60), "brightness": 45, "effect": 0},
    {"id": "night", "name": "сон", "icon": "moon", "color": (60, 20, 120), "brightness": 12, "effect": 0},
    {"id": "movie", "name": "кино", "icon": "clapperboard", "color": (120, 20, 180), "brightness": 15, "effect": 142},
    {"id": "game", "name": "игры", "icon": "gamepad-2", "color": (0, 200, 255), "brightness": 70, "effect": 148},
    {"id": "party", "name": "тусовка", "icon": "party-popper", "color": (255, 0, 128), "brightness": 90, "effect": 136},
    {"id": "focus", "name": "фокус", "icon": "target", "color": (0, 255, 160), "brightness": 80, "effect": 0},
    {"id": "relax", "name": "релакс", "icon": "waves", "color": (0, 120, 255), "brightness": 30, "effect": 146},
    {"id": "alarm", "name": "будильник", "icon": "alarm-clock", "color": (255, 255, 255), "brightness": 100, "effect": 156},
    {"id": "disco", "name": "диско", "icon": "disc-3", "color": (255, 0, 200), "brightness": 80, "effect": 145},
    {"id": "aurora", "name": "северное сияние", "icon": "waves", "color": (0, 255, 200), "brightness": 55, "effect": 143},
]


def _load(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                            # noqa: BLE001
        return default


def _save(path: str, data: Any) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


@dataclass
class Preset:
    name: str
    r: int
    g: int
    b: int
    brightness: int = 100
    effect: int = 0
    speed: int = 50
    builtin: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["hex"] = f"#{self.r:02x}{self.g:02x}{self.b:02x}"
        return d


DEFAULT_PRESETS = [
    Preset("белый", 255, 255, 255, 100, 0, 50, True),
    Preset("тёплый", 255, 180, 110, 70, 0, 50, True),
    Preset("закат", 255, 90, 60, 65, 0, 50, True),
    Preset("ночной", 40, 60, 255, 20, 0, 50, True),
    Preset("неон", 0, 255, 200, 90, 0, 50, True),
    Preset("фиолетовый", 140, 0, 255, 80, 0, 50, True),
    Preset("розовый", 255, 60, 180, 80, 0, 50, True),
    Preset("мята", 60, 255, 160, 60, 0, 50, True),
    Preset("космос", 30, 0, 90, 40, 0, 50, True),
    Preset("огонь", 255, 60, 0, 85, 0, 50, True),
]


@dataclass
class Rule:
    """правило автоматизации."""
    id: str
    time: str                       # "07:30" или "sunset" / "sunrise"
    action: dict[str, Any]          # {"kind": "color"|"power"|"brightness"|"scene"|"effect", ...}
    days: int = 0x7F
    enabled: bool = True
    name: str = ""
    fade_ms: int = 400

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Store:
    def __init__(self):
        self.presets: list[Preset] = [
            Preset(**p) for p in _load(PRESETS_FILE, [asdict(p) for p in DEFAULT_PRESETS])
        ] or list(DEFAULT_PRESETS)
        # правила лежат на диске словарями, а дальше по коду идут как объекты.
        # раньше сюда клался сырой список dict, и list_rules() падал с
        # AttributeError: 'dict' object has no attribute 'to_dict'
        self.rules: list[Rule] = [
            r if isinstance(r, Rule) else Rule(**{k: v for k, v in r.items()
                                                  if k in Rule.__dataclass_fields__})
            for r in _load(RULES_FILE, [])
        ]
        self.settings: dict[str, Any] = _load(SETTINGS_FILE, {
            "variant": "generic",
            "mac": "AA:BB:CC:DD:EE:FF",
            "accent": "#3cec80",
            "auto_connect": True,
        })
        self._fired: dict[str, float] = {}

    # ── пресеты ─────────────────────────────────────────────────────
    def list_presets(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self.presets]

    def add_preset(self, name: str, r: int, g: int, b: int,
                   brightness: int = 100, effect: int = 0, speed: int = 50) -> dict[str, Any]:
        p = Preset(name.strip() or f"пресет {len(self.presets) + 1}",
                   r, g, b, brightness, effect, speed)
        self.presets.append(p)
        self.save_presets()
        return p.to_dict()

    def update_preset(self, idx: int, **kw: Any) -> dict[str, Any] | None:
        if not 0 <= idx < len(self.presets):
            return None
        p = self.presets[idx]
        for k, v in kw.items():
            if hasattr(p, k) and k != "builtin":
                setattr(p, k, v)
        self.save_presets()
        return p.to_dict()

    def remove_preset(self, idx: int) -> bool:
        if 0 <= idx < len(self.presets) and not self.presets[idx].builtin:
            self.presets.pop(idx)
            self.save_presets()
            return True
        return False

    def save_presets(self) -> None:
        _save(PRESETS_FILE, [asdict(p) for p in self.presets])

    def reorder_presets(self, order: list[int]) -> None:
        pool = list(self.presets)
        self.presets = [pool[i] for i in order if 0 <= i < len(pool)]
        self.save_presets()

    # ── правила ─────────────────────────────────────────────────────
    def list_rules(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.rules]

    def add_rule(self, rule: Rule) -> dict[str, Any]:
        self.rules.append(rule)
        _save(RULES_FILE, [r.to_dict() for r in self.rules])
        return rule.to_dict()

    def remove_rule(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        _save(RULES_FILE, [r.to_dict() for r in self.rules])
        return len(self.rules) < before

    def toggle_rule(self, rule_id: str, enabled: bool) -> bool:
        for r in self.rules:
            if r.id == rule_id:
                r.enabled = enabled
                _save(RULES_FILE, [r.to_dict() for r in self.rules])
                return True
        return False
