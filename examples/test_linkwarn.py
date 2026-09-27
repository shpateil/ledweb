"""живая проверка: панель обязана показывать баннер, когда лента не на связи.

сценарий: рвём ble снаружи, ждём снимок состояния, проверяем что баннер виден
и кнопка реконнекта есть. потом ждём авто-восстановления и проверяем, что баннер
исчез. без проверок на выход — тест не считается.
"""
import json
import subprocess
import sys
import time
import urllib.request

import websocket

TAB = "http://127.0.0.1:8123/"
CDP = "http://127.0.0.1:9222"
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


def api(path):
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:8123{path}", timeout=6).read())


def state():
    return api("/api/state")


def open_tab():
    tabs = json.loads(urllib.request.urlopen(f"{CDP}/json", timeout=6).read())
    for t in tabs:
        if t.get("type") == "page" and "127.0.0.1:8123" in t.get("url", ""):
            try:
                urllib.request.urlopen(f"{CDP}/json/close/{t['id']}", timeout=3)
            except Exception:
                pass
    time.sleep(0.6)
    # в свежем chrome /json/new требует PUT, GET даёт 405
    req = urllib.request.Request(f"{CDP}/json/new?{TAB}", method="PUT")
    r = urllib.request.urlopen(req, timeout=12)
    return json.loads(r.read())["webSocketDebuggerUrl"]


PROBE = (
    "(() => { const b = document.getElementById('link-warn');"
    " return { exists: !!b, hidden: b ? b.hidden : null,"
    " text: b ? (document.getElementById('link-warn-text')||{}).textContent : null,"
    " btn: !!document.getElementById('btn-reconnect'),"
    " icons: document.querySelectorAll('svg').length,"
    " scenes: document.querySelectorAll('.scene').length,"
    " errs: (window.__errs||[]).length }; })()"
)


class Cdp:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=25)
        self.n = 10          # id ниже 10 занят под Runtime.enable
        self.ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        while True:
            if json.loads(self.ws.recv()).get("id") == 1:
                break

    def recv(self):
        while True:
            d = json.loads(self.ws.recv())
            if d.get("method") == "Runtime.exceptionThrown":
                det = d["params"]["exceptionDetails"]
                try:
                    self._last_exc = det["exception"].get("description", "")[:200]
                except Exception:
                    self._last_exc = str(det)[:200]
                self._exc_flag = True
            if d.get("id") == self.n:
                return d

    def js(self, expr):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": "Runtime.evaluate",
                                 "params": {"expression": expr, "returnByValue": True}}))
        while True:
            d = json.loads(self.ws.recv())
            if d.get("id") == self.n:
                return d.get("result", {}).get("result", {}).get("value")


def wait_state(pred, limit):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            s = state()
            if pred(s):
                return s, round(time.time() - t0, 1)
        except Exception:
            pass
        time.sleep(0.7)
    return None, limit


def main():
    # cdp должен быть уже поднят снаружи. ВАЖНО: не звать тут
    # pkill -f 'remote-debugging-port=9222' — паттерн совпадает с cmdline
    # самого шелла, и pkill убивает его вместе с собой (мы это проходили)
    for _ in range(20):
        try:
            urllib.request.urlopen(f"{CDP}/json/version", timeout=3)
            break
        except Exception:
            time.sleep(1)
    else:
        print("  ПРОВАЛ cdp на 9222 не отвечает, запусти chromium и повтори")
        return 1

    print("── старт ──")
    cdp = Cdp(open_tab())
    time.sleep(4)
    v = cdp.js(PROBE)
    check("страница загрузилась", v.get("scenes", 0) > 0, f"сцен {v.get('scenes')}")
    # ВАЖНО: createIcons заменяет <i data-lucide> на <svg>, поэтому
    # '[data-lucide] svg' всегда пусто. считаем сами svg
    check("иконки отрисованы", v.get("icons", 0) > 10, f"{v.get('icons')}")
    check("js-ошибок нет", v.get("errs", 0) == 0, f"{v.get('errs')}")
    check("баннер спрятан при связи", v.get("hidden") is True)
    check("кнопка реконнекта есть", v.get("btn") is True)

    print("\n── рвём ble снаружи ──")
    # disconnect не ждём: bluetoothctl умеет висеть, если адаптер занят демоном
    subprocess.Popen(["bluetoothctl", "disconnect", "be:30:70:00:00:36"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # смотрим снимок быстро: демену хватит секунды увидеть обрыв
    got = False
    t0 = time.time()
    while time.time() - t0 < 6:
        v = cdp.js(PROBE)
        if v.get("hidden") is False:
            got = True
            break
        time.sleep(0.3)
    check("баннер показался при обрыве", got,
          f"hidden={v.get('hidden')} текст={v.get('text')!r}")
    check("текст объясняет обрыв", bool(v.get("text")) and "связ" in (v.get("text") or ""),
          v.get("text"))

    print("\n── ждём авто-восстановления ──")
    s, took = wait_state(lambda s: s.get("connected") is True, 40)
    check("демон переподключился сам", s is not None, f"{took} с")
    gone = False
    t0 = time.time()
    while time.time() - t0 < 8:
        v = cdp.js(PROBE)
        if v.get("hidden") is True:
            gone = True
            break
        time.sleep(0.3)
    check("баннер исчез после реконнекта", gone, f"hidden={v.get('hidden')}")
    check("js-ошибок не набежало", v.get("errs", 0) == 0, f"{v.get('errs')}")

    print("\n── команда проходит после реконнекта ──")
    st = api("/api/state")
    hexv = st["color"]["hex"]
    print(f"  (текущий цвет {hexv})")
    check("связь на месте", st.get("connected") is True)

    print(f"\nитог: {ok} ок, {len(bad)} провалов")
    if bad:
        print("провалились: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
