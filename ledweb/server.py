"""http-сервер ledweb: rest api + sse + статика. только стандартная библиотека.

без flask/fastapi — меньше зависимостей, мгновенный старт, ничего не тянется из сети.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import socket
import time
import uuid
from http import HTTPStatus
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import fx as FX
from . import protocol as P
from .driver import Device, Driver
from .fx import Engine
from .store import SCENES, Store

log = logging.getLogger("ledweb.server")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

MAX_BODY = 64 * 1024
# сколько ждём следующий запрос на keep-alive сокетете перед тихим закрытием
IDLE_TIMEOUT = 30.0
# дополнительные origin через запятую: LEDWEB_ORIGINS=https://led.example.com
ALLOWED_ORIGINS = {
    o.strip().rstrip("/")
    for o in os.environ.get("LEDWEB_ORIGINS", "").split(",")
    if o.strip()
}


class SSE:
    """простой broadcaster для server-sent events."""

    def __init__(self) -> None:
        self._clients: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    def broadcast(self, event: str, data: Any) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for q in list(self._clients):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                self._clients.discard(q)


Handler = Callable[[dict, "Request"], Awaitable[Any]]


class Request:
    def __init__(self, method: str, path: str, query: dict, body: bytes, headers: dict):
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.headers = headers

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:                        # noqa: BLE001
            return {}


class Response:
    def __init__(self, status: int = 200, body: bytes = b"", content_type: str = "application/json",
                 headers: dict | None = None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}


def _rgb_from_body(d: dict[str, Any]) -> tuple[int, int, int]:
    """достаёт цвет из тела запроса: rgb напрямую либо hsv через protocol."""
    if "r" in d:
        return (max(0, min(255, int(d.get("r", 0)))),
                max(0, min(255, int(d.get("g", 0)))),
                max(0, min(255, int(d.get("b", 0)))))
    from .protocol import hsv_to_rgb
    return hsv_to_rgb(float(d.get("h", 0)), float(d.get("s", 1.0)), float(d.get("v", 1.0)))


def _days_to_mask(days: Any) -> int:
    """фронт шлёт список [0..6] (0=пн), прошивка ждёт битовую маску.

    бит i = день i. принимаем и список, и уже готовую маску.
    """
    if isinstance(days, int):
        return days & 0xFF
    if isinstance(days, (list, tuple)):
        mask = 0
        for d in days:
            try:
                n = int(d)
            except (TypeError, ValueError):
                continue
            # без проверки диапазона days=[10**10] давал 1 << 10**10 — это
            # 1.2 гигабайта на один запрос, и демон умирал по MemoryMax
            if not 0 <= n <= 7:
                continue
            mask |= 1 << n
        return mask
    return 0x7F


def jsonr(data: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")


class Server:
    def __init__(self, driver: Driver, store: Store, host: str = "127.0.0.1", port: int = 8099):
        self.driver = driver
        self.store = store
        self.host = host
        self.port = port
        self.sse = SSE()
        self.routes: dict[tuple[str, str], Handler] = {}
        self._server: asyncio.Server | None = None
        self.started_at = time.time()
        # софтверный движок эффектов: шлёт цвет через обычную очередь драйвера
        self.fx = Engine(self._fx_send)
        # таймер выключения: unix-время, когда лента должна погаснуть
        self._off_at: float | None = None
        self._off_task: asyncio.Task | None = None
        # флаг остановки: keep-alive цикл держит соединения, их надо оборвать
        self._stopped = False
        driver.on_change = self._on_state
        self._register()

    async def _fx_send(self, color: tuple[int, int, int]) -> None:
        await self.driver.set_color(*color)

    def _on_state(self, state) -> None:
        self.sse.broadcast("state", self.driver.snapshot())

    # ── маршруты ────────────────────────────────────────────────────
    def route(self, method: str, path: str):
        def deco(fn: Handler) -> Handler:
            self.routes[(method, path)] = fn
            return fn
        return deco

    # ── таймер выключения ───────────────────────────────────────────
    def timer_state(self) -> dict[str, Any]:
        left = int(max(0.0, self._off_at - time.time())) if self._off_at else 0
        return {"armed": self._off_at is not None, "left": left,
                "at": self._off_at}

    def set_timer(self, minutes: float | None) -> dict[str, Any]:
        """minutes=None снимает таймер. 0 тоже снимает."""
        old, self._off_task = self._off_task, None
        if old is not None:
            old.cancel()
        if minutes is None or minutes <= 0:
            self._off_at = None
            self.sse.broadcast("timer", self.timer_state())
            return self.timer_state()
        self._off_at = time.time() + minutes * 60.0
        self._off_task = asyncio.create_task(self._fire_timer(), name="off-timer")
        self.sse.broadcast("timer", self.timer_state())
        log.info("таймер выключения через %s мин", minutes)
        return self.timer_state()

    async def _fire_timer(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._off_at is None:
                    return
                if time.time() >= self._off_at:
                    self._off_at = None
                    self._off_task = None
                    self.fx.stop()
                    await self.driver.set_power(False)
                    self.sse.broadcast("timer", self.timer_state())
                    log.info("таймер сработал, лента выключена")
                    return
                self.sse.broadcast("timer", self.timer_state())
        except asyncio.CancelledError:
            raise

    def _register(self) -> None:
        r = self.route

        @r("GET", "/api/state")
        async def _state(_q, _rq):
            return jsonr(self.driver.snapshot())

        @r("GET", "/api/meta")
        async def _meta(_q, _rq):
            return jsonr({
                "effects": [
                    {"value": int(e), "name": P.EFFECT_NAMES_RU.get(int(e), f"режим {int(e):#x}")}
                    for e in P.Effect
                ],
                "palettes": {
                    "material": [{"name": n, "hex": f"#{r:02x}{g:02x}{b:02x}",
                                  "tints": [f"#{tr:02x}{tg:02x}{tb:02x}" for _, tr, tg, tb in P.PALETTE_MATERIAL
                                            if _ == n]}
                                 for n, r, g, b in P.PALETTE_MATERIAL],
                    "mood": [{"name": n, "hex": f"#{r:02x}{g:02x}{b:02x}",
                              "tints": [f"#{tr:02x}{tg:02x}{tb:02x}" for _, tr, tg, tb in P.PALETTE_MOOD
                                        if _ == n]}
                             for n, r, g, b in P.PALETTE_MOOD],
                },
                    "scenes": [dict(s, color=f"#{s['color'][0]:02x}{s['color'][1]:02x}{s['color'][2]:02x}")
                           for s in SCENES],
                "variants": list(P.VARIANTS.keys()),
                "fx": [{"key": k, "name": FX.EFFECTS[k]["name"], "fps": FX.EFFECTS[k]["fps"]}
                       for k in FX.ORDER],
                "fx_state": self.fx.state(),
                "timer": self.timer_state(),
                "weekdays": [{"value": int(d), "name": n} for n, d in [
                    ("пн", P.WeekDay.mon), ("вт", P.WeekDay.tue), ("ср", P.WeekDay.wed),
                    ("чт", P.WeekDay.thu), ("пт", P.WeekDay.fri), ("сб", P.WeekDay.sat),
                    ("вс", P.WeekDay.sun)]],
            })

        @r("POST", "/api/apply")
        async def _apply(_q, rq):
            data = rq.json()
            action = data.get("action", "")
            # прямая команда цвета/яркости/сцены гасит софтверный эффект:
            # иначе цикл продолжит перекрашивать ленту поверх выбора юзера
            if action in ("color", "brightness", "power", "scene"):
                if not (action == "color" and data.get("fx")):
                    self.fx.stop()
            # одна попытка: реконнектом занимается фоновой поток драйвера,
            # из хендлера его трогать нельзя — будет гонка
            try:
                return jsonr(await self.driver.apply(action, **data))
            except Exception as e:                # noqa: BLE001
                log.warning("apply %s: %s", action, e)
                return jsonr({"error": str(e), "type": type(e).__name__}, 502)

        @r("POST", "/api/fx")
        async def _fx(_q, rq):
            """софтверный эффект. ключ из /api/meta .fx, base — текущий цвет."""
            d = rq.json()
            key = d.get("key", "static")
            if key not in FX.EFFECTS:
                return jsonr({"error": f"нет эффекта {key}"}, 404)
            if key == "static":
                self.fx.stop()
                return jsonr({"fx": self.fx.state()})
            st = self.driver.state
            # state.color — кортеж (r,g,b), а не отдельные поля
            base = tuple(st.color)
            ok = self.fx.start(key, base, float(d.get("speed", 1.0)))
            if not ok:
                return jsonr({"error": "эффект не запустился"}, 500)
            self.sse.broadcast("fx", self.fx.state())
            return jsonr({"fx": self.fx.state()})

        @r("POST", "/api/fx/stop")
        async def _fx_stop(_q, _rq):
            self.fx.stop()
            self.sse.broadcast("fx", self.fx.state())
            return jsonr({"fx": self.fx.state()})

        @r("GET", "/api/timer")
        async def _timer_get(_q, _rq):
            return jsonr(self.timer_state())

        @r("POST", "/api/timer")
        async def _timer_set(_q, rq):
            d = rq.json()
            return jsonr(self.set_timer(d.get("minutes")))

        @r("DELETE", "/api/timer")
        async def _timer_off(_q, _rq):
            return jsonr(self.set_timer(None))

        @r("POST", "/api/scene")
        async def _scene(_q, rq):
            sid = rq.json().get("id")
            scene = next((s for s in SCENES if s["id"] == sid), None)
            if not scene:
                return jsonr({"error": "нет такой сцены"}, 404)
            d = self.driver
            r, g, b = scene["color"]
            await d.set_color(r, g, b)
            await d.set_brightness(scene["brightness"])
            if scene["effect"]:
                await d.set_effect(scene["effect"])
            return jsonr(self.driver.snapshot())

        @r("GET", "/api/presets")
        async def _presets(_q, _rq):
            return jsonr(self.store.list_presets())

        @r("POST", "/api/presets")
        async def _add_preset(_q, rq):
            d = rq.json()
            # фронт шлёт цвет в hsv, пресет хранит rgb — конвертим на входе,
            # иначе пресет сохранялся белым (дефолт 255/255/255)
            r, g, b = _rgb_from_body(d)
            return jsonr(self.store.add_preset(d.get("name", ""), r, g, b,
                                               d.get("brightness", 100),
                                               d.get("effect", 0), d.get("speed", 50)))

        @r("PATCH", "/api/presets")
        async def _upd_preset(_q, rq):
            d = rq.json()
            patch = {k: v for k, v in d.items() if k != "index"}
            if "h" in patch and "r" not in patch:
                patch["r"], patch["g"], patch["b"] = _rgb_from_body(patch)
            res = self.store.update_preset(int(d.get("index", -1)), **patch)
            return jsonr(res or {"error": "нет такого пресета"}, 404 if res is None else 200)

        @r("DELETE", "/api/presets")
        async def _del_preset(_q, rq):
            idx = int(_q.get("index", ["-1"])[0])
            return jsonr({"ok": self.store.remove_preset(idx)})

        @r("GET", "/api/rules")
        async def _rules(_q, _rq):
            return jsonr(self.store.list_rules())

        @r("POST", "/api/rules")
        async def _add_rule(_q, rq):
            from .store import Rule
            d = rq.json()
            # фронт шлёт action плоским (action + h/s/v рядом), а не объектом
            act = d.get("action")
            if isinstance(act, dict):
                payload = {**act, **{k: v for k, v in d.items() if k != "action"}}
            else:
                payload = dict(d)
            if payload.get("action") == "color":
                r, g, b = _rgb_from_body(payload)
                payload.update(r=r, g=g, b=b)
            rule = Rule(id=d.get("id") or uuid.uuid4().hex[:8], time=d.get("time", "07:00"),
                        action=payload, days=_days_to_mask(d.get("days", 0x7F)),
                        enabled=bool(d.get("enabled", True)), name=d.get("name", ""),
                        fade_ms=int(d.get("fade_ms", 400)))
            return jsonr(self.store.add_rule(rule))

        @r("DELETE", "/api/rules")
        async def _del_rule(_q, rq):
            rid = _q.get("id", [""])[0]
            return jsonr({"ok": self.store.remove_rule(rid)})

        @r("GET", "/api/scan")
        async def _scan(_q, _rq):
            try:
                return jsonr({"devices": await self.driver.scan(timeout=4.0)})
            except Exception as e:                # noqa: BLE001
                return jsonr({"error": str(e), "devices": []}, 502)

        @r("GET", "/api/schedule-read")
        async def _sched(_q, _rq):
            return jsonr({"raw": await self.driver.query_schedule()})

        @r("POST", "/api/reconnect")
        async def _reconnect(_q, rq):
            # переподключением владеет фоновой поток драйвера. форсируем его
            # перезапуск, а не дёргаем соединение из хендлера
            self.driver.dev.auto_connect = False
            await asyncio.sleep(0.3)
            self.driver.dev.auto_connect = True
            return jsonr(self.driver.snapshot())

        @r("GET", "/api/settings")
        async def _settings(_q, _rq):
            return jsonr(self.store.settings)

        @r("POST", "/api/settings")
        async def _set_settings(_q, rq):
            self.store.settings.update(rq.json())
            self._save_settings()
            return jsonr(self.store.settings)

        @r("GET", "/api/health")
        async def _health(_q, _rq):
            return jsonr({"ok": True, "connected": self.driver.connected,
                          "uptime": round(time.time() - self.started_at, 1)})

    def _save_settings(self) -> None:
        from .store import _save, SETTINGS_FILE
        _save(SETTINGS_FILE, self.store.settings)

    # ── http ────────────────────────────────────────────────────────
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # без nodelay sse-события склеиваются пакетами по 40 мс и с задержкой
        with contextlib.suppress(Exception):
            writer.get_extra_info("socket").setsockopt(
                socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            # keep-alive: держим соединение и крутим цикл чтения. замер показал
            # 1665 rps против 335 с Connection: close. закрываем только когда
            # клиент сам ушёл или попросил close
            while not self._stopped:
                if not await self._one(reader, writer):
                    break
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception:                        # noqa: BLE001
            log.exception("http error")
        finally:
            # await wait_closed() в питоне 3.12+ виснет, пока пира не закроет
            # свою сторону — это вешало event loop и копило CLOSE-WAIT
            try:
                writer.close()
            except Exception:                    # noqa: BLE001
                pass

    async def _one(self, reader: asyncio.StreamReader,
                   writer: asyncio.StreamWriter) -> bool:
        """один запрос-ответ. вернёт False когда соединение пора закрывать."""
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=IDLE_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            return False
        if not line:
            return False
        try:
            method, target, _ = line.decode(errors="replace").split(" ", 2)
        except ValueError:
            return False
        headers: dict[str, str] = {}
        while True:
            try:
                hl = await asyncio.wait_for(reader.readline(), timeout=10)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return False
            if not hl or hl in (b"\r\n", b"\n"):
                break
            k, _, v = hl.decode(errors="replace").partition(":")
            headers[k.strip().lower()] = v.strip()

        body = b""
        try:
            n = int(headers.get("content-length", 0) or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            await self._write(writer, jsonr({"error": "тело слишком большое"},
                                            HTTPStatus.REQUEST_ENTITY_TOO_LARGE))
            return False
        if n:
            with contextlib.suppress(asyncio.IncompleteReadError):
                body = await asyncio.wait_for(reader.readexactly(n), timeout=10)

        url = urlparse(target)
        path = unquote(url.path)
        query = parse_qs(url.query)

        # sse держит соединение до конца стрима и сам решает, когда закрыть
        if path == "/api/stream":
            await self._stream(reader, writer)
            return False

        # префлайт для браузерных кросс-доменных запросов
        if method == "OPTIONS":
            resp = Response(HTTPStatus.NO_CONTENT, b"", "text/plain")
            return await self._finish(writer, resp, headers)

        resp = await self._dispatch(method, path, query, body, headers)
        return await self._finish(writer, resp, headers)

    async def _finish(self, writer: asyncio.StreamWriter, resp: Response,
                      headers: dict[str, str]) -> bool:
        keep = headers.get("connection", "").lower() != "close"
        await self._write(writer, resp, keep_alive=keep, origin=headers.get("origin"))
        return keep

    async def _dispatch(self, method: str, path: str, query: dict, body: bytes,
                        headers: dict) -> Response:
        fn = self.routes.get((method, path))
        if fn is None:
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            return jsonr({"error": "not found", "path": path}, HTTPStatus.NOT_FOUND)
        rq = Request(method, path, query, body, headers)
        try:
            res = await fn(query, rq)
        except Exception as e:                    # noqa: BLE001
            log.exception("route %s failed", path)
            return jsonr({"error": str(e), "type": type(e).__name__}, 500)
        return res if isinstance(res, Response) else jsonr(res)

    def _static(self, path: str) -> Response:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        # разметка ссылается на /static/..., а STATIC_DIR уже указывает на static/.
        # без среза префикса путь склеивался в static/static/... и любой файл
        # отдавался как index.html с типом text/html — браузер молча выкидывал стили.
        if rel == "static" or rel.startswith("static/"):
            rel = rel[len("static"):].lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            full = os.path.join(STATIC_DIR, "index.html")
            if not os.path.isfile(full):
                return jsonr({"error": "нет статики"}, 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            data = f.read()
        mtime = int(os.path.getmtime(full))
        return Response(200, data, ctype, {"Cache-Control": "no-cache"})

    def _cors(self, origin: str | None) -> dict[str, str]:
        """cors без дыры: `*` на localhost читается любым сайтом из браузера.

        разрешаем только сам localhost и origin из переменной окружения —
        так панель можно открыть с телефона через туннель, не открывая её всему
        интернету. иначе origin не отдаём вообще.
        """
        out = {"Vary": "Origin"}
        if not origin:
            return out
        host = urlparse(origin).hostname or ""
        if host in ("127.0.0.1", "localhost", "::1") or origin in ALLOWED_ORIGINS:
            out["Access-Control-Allow-Origin"] = origin
            out["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
            out["Access-Control-Allow-Headers"] = "Content-Type"
        return out

    async def _write(self, writer: asyncio.StreamWriter, resp: Response,
                     keep_alive: bool = False, origin: str | None = None) -> None:
        head = [
            f"HTTP/1.1 {resp.status} {HTTPStatus(resp.status).phrase}",
            f"Content-Type: {resp.content_type}",
            f"Content-Length: {len(resp.body)}",
            "Cache-Control: no-cache",
            "X-Content-Type-Options: nosniff",
            # keep-alive держит соединение между запросами: с ним было 1665 rps,
            # с close 335. sse-ответ всё равно уходит первым
            "Connection: " + ("keep-alive" if keep_alive else "close"),
        ]
        for k, v in self._cors(origin).items():
            head.append(f"{k}: {v}")
        for k, v in resp.headers.items():
            head.append(f"{k}: {v}")
        # один write на head+body: два write'а давали 41мс из-за nagle+delayed ack
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + resp.body)
        await writer.drain()

    async def _stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Cache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
        await writer.drain()
        q = self.sse.subscribe()
        try:
            snap = self.driver.snapshot()
            writer.write(f"event: state\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n".encode())
            await writer.drain()
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    payload = ": ping\n\n"
                writer.write(payload.encode() if isinstance(payload, str) else payload)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self.sse.unsubscribe(q)
            try:
                writer.close()
            except Exception:                    # noqa: BLE001
                pass

    async def run(self) -> None:
        self._server = await asyncio.start_server(
            self.handle, self.host, self.port,
            reuse_address=True, backlog=256)
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        log.info("ledweb слушает %s", addrs)
        async with self._server:
            await self._server.serve_forever()

    def close(self) -> None:
        """гасим фоновые циклы. без этого systemd на stop уходит в SIGKILL
        по таймауту: задачи висят на ble и не отдают управление."""
        self._stopped = True
        self.fx.stop()
        t, self._off_task = self._off_task, None
        if t is not None:
            t.cancel()


def build(host: str = "127.0.0.1", port: int = 8099) -> Server:
    store = Store()
    dev = Device(mac=store.settings.get("mac", P.DEFAULT_MAC),
                 variant=store.settings.get("variant", P.DEFAULT_VARIANT),
                 auto_connect=store.settings.get("auto_connect", True))
    driver = Driver(dev)
    return Server(driver, store, host, port)
