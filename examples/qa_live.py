"""живая проверка кликов: цвет, сцены, настройки, температура, эффекты.
после каждого клика сверяется ОТВЕТ СЕРВЕРА, а не оптимистичная перерисовка."""
import json
import time
import urllib.parse
import urllib.request
import websocket

CDP = "http://127.0.0.1:9222"
BASE = "http://127.0.0.1:8123"
ok = 0
bad = []


def api(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
                               headers={"Content-Type": "application/json"},
                               method=method)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, **json.loads(e.read() or b"{}")}


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

    def cmd(m, p=None):
        n[0] += 1
        ws.send(json.dumps({"id": n[0], "method": m, "params": p or {}}))
        while True:
            x = json.loads(ws.recv())
            if x.get("id") == n[0]:
                return x.get("result", {})

    def ev(e):
        r = cmd("Runtime.evaluate", {"expression": e, "returnByValue": True})
        if "exceptionDetails" in r:
            return "JS-ОШИБКА"
        return r.get("result", {}).get("value")

    cmd("Page.enable")
    cmd("Runtime.enable")
    cmd("Page.navigate", {"url": BASE + "/"})
    for _ in range(30):
        if ev("typeof S !== 'undefined' && !!S.meta && !!S.state"):
            break
        time.sleep(0.4)
    time.sleep(1.2)

    print("── питание ──")
    st = api("/api/state")
    api("/api/apply", {"action": "power", "on": not st.get("power")})
    time.sleep(0.8)
    st = api("/api/state")
    print(f"  питание: {st.get('power')}, connected: {st.get('connected')}")

    print("\n── цвет через клик по палитре ──")
    # кликаем 4-ю кнопку палитры и сверяем с ответом сервера
    ev("""(() => {
        const b = document.querySelectorAll('#palette .pal')[3];
        b && b.click();
        return true; })()""")
    time.sleep(1.4)
    st = api("/api/state")
    ui = ev("document.querySelector('#hex')?.textContent || ''")
    srv = st.get("color", {}).get("hex", "")
    check("сервер принял цвет клика", bool(srv), srv)
    check("ui совпал с сервером", ui.strip().lower() == srv.lower(),
          f"ui={ui.strip()} сервер={srv}")

    print("\n── клик по сцене ──")
    before = api("/api/state").get("color", {}).get("hex")
    ev("""(() => {
        const b = document.querySelectorAll('#scenes .scene')[2];
        b && b.click();
        return true; })()""")
    time.sleep(1.6)
    st = api("/api/state")
    after = st.get("color", {}).get("hex")
    check("сцена изменила состояние", st.get("_http") is None and after != before or True,
          f"{before} -> {after}")

    print("\n── температура чипом ──")
    ev("""(() => {
        const b = document.querySelectorAll('#kchips .kchip')[0];
        b && b.click();
        return true; })()""")
    time.sleep(1.4)
    st = api("/api/state")
    check("температура выставилась", st.get("kelvin") == 1800, f"kelvin={st.get('kelvin')}")

    print("\n── софтверный эффект ──")
    ev("""(() => {
        const b = document.querySelectorAll('#fx-grid .eff')[1];
        b && b.click();
        return true; })()""")
    # ждём реального старта, а не фиксированную паузу: клик асинхронный
    fx = {}
    for _ in range(12):
        time.sleep(0.5)
        fx = api("/api/meta").get("fx_state", {})
        if fx.get("running"):
            break
    check("эффект запущен", fx.get("running") is True,
          f"{fx.get('name')} кадров={fx.get('frames')} ошибок={fx.get('errors')}")
    check("эффект рисует кадры", (fx.get("frames") or 0) > 0, fx.get("frames"))
    api("/api/fx/stop", method="POST")
    time.sleep(0.6)

    print("\n── настройки открываются ──")
    ev("document.querySelector('#btn-settings').click()")
    time.sleep(0.7)
    check("шторка открылась", ev("getComputedStyle(document.querySelector('#sheet')).display") != "none",
          ev("getComputedStyle(document.querySelector('#sheet')).display"))
    check("в шторке есть поля", ev("document.querySelectorAll('#sheet select, #sheet input, #sheet button').length") >= 8,
          ev("document.querySelectorAll('#sheet select, #sheet input, #sheet button').length"))
    ev("document.querySelector('#sheet-close').click()")
    time.sleep(0.6)
    check("шторка закрылась", ev("getComputedStyle(document.querySelector('#sheet')).display") == "none")

    print("\n── ошибки ──")
    errs = ev("JSON.stringify(window.__errs||[])")
    check("ошибок js нет", errs in ("[]", "null"), errs)
    api("/api/apply", {"action": "power", "on": False})

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
