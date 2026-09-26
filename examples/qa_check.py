"""живая проверка после правок: скролл, скрытая шторка, чипы температуры."""
import json
import time
import urllib.parse
import urllib.request
import websocket

CDP = "http://127.0.0.1:9222"
BASE = "http://127.0.0.1:8123"
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


def main():
    req = urllib.request.Request(
        CDP + "/json/new?" + urllib.parse.quote(BASE + "/", safe=""), method="PUT")
    tgt = json.loads(urllib.request.urlopen(req, timeout=10).read())
    ws = websocket.create_connection(tgt["webSocketDebuggerUrl"], timeout=40)
    n = [0]

    def cmd(method, params=None):
        n[0] += 1
        ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == n[0]:
                return m.get("result", {})

    def ev(expr):
        r = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        if "exceptionDetails" in r:
            return "JS-ОШИБКА"
        return r.get("result", {}).get("value")

    cmd("Page.enable")
    cmd("Runtime.enable")
    cmd("Page.navigate", {"url": BASE + "/"})
    for _ in range(30):
        if ev("typeof S !== 'undefined' && !!S.meta"):
            break
        time.sleep(0.4)
    time.sleep(1.5)

    print("\n── скролл ──")
    h0 = ev("window.scrollY")
    ev("window.scrollTo(0, 600)")
    time.sleep(0.4)
    h1 = ev("window.scrollY")
    check("страница листается", h1 > h0, f"{h0} -> {h1}")

    # колесо над панелью не должно съедать скролл
    ev("window.scrollTo(0,0)")
    time.sleep(0.3)
    w = ev("""(() => {
        const el = document.querySelector('.palette') || document.querySelector('main');
        const ev2 = new WheelEvent('wheel', {deltaY: 300, bubbles: true, cancelable: true});
        el.dispatchEvent(ev2);
        return ev2.defaultPrevented;
    })()""")
    check("колесо по палитре НЕ перехвачено", w is False, f"defaultPrevented={w}")

    print("\n── шторка настроек ──")
    sheet_open = ev("getComputedStyle(document.querySelector('#sheet')).display !== 'none'")
    check("шторка скрыта при загрузке", not sheet_open, f"display={ev("getComputedStyle(document.querySelector('#sheet')).display")}")
    vis = ev("""(() => {
        const r = document.querySelector('#sheet').getBoundingClientRect();
        return document.elementFromPoint(window.innerWidth - 60, 60)?.id || '';
    })()""")
    check("шторка не перекрывает панель", vis != "sheet", f"поверх: {vis or 'ничего'}")

    toff = ev("getComputedStyle(document.querySelector('#t-off')).display")
    check("кнопка отмены таймера скрыта", toff == "none", f"display={toff}")

    print("\n── звук вырезан ──")
    check("нет карточки микрофона", ev("!document.querySelector('#mic')"))
    check("нет miclevel", ev("!document.querySelector('#miclevel')"))
    check("нет mic в тексте", "реакция на звук" not in (ev("document.body.innerText") or ""))

    print("\n── температура ──")
    chips = ev("document.querySelectorAll('#kchips .kchip').length")
    check("чипы пресетов отрисованы", chips == 7, f"{chips} шт")
    names = ev("""[...document.querySelectorAll('#kchips .kchip')].map(b => b.textContent).join('|')""")
    check("названия на русском", "свеча" in (names or ""), (names or "")[:70])

    print("\n── контент ──")
    check("сцены", ev("document.querySelectorAll('#scenes .scene').length") >= 12)
    check("софт-эффекты", ev("document.querySelectorAll('#fx-grid .eff').length") == 11)
    check("встроенные эффекты", ev("document.querySelectorAll('#effects .eff').length") > 20)
    over = ev("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("нет горизонтального переполнения", over <= 0, f"перебор {over}px")
    errs = ev("JSON.stringify(window.__errs||[])")
    check("ошибок js нет", errs in ("[]", "null"), errs)

    ws.close()
    try:
        urllib.request.urlopen(CDP + "/json/close/" + tgt["id"], timeout=5)
    except Exception:
        pass
    print(f"\nитог: {ok} ок, {len(bad)} провалов")
    if bad:
        print("провалились: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
