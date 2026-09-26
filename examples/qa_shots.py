"""скриншоты панели в разных вьюпортах + клик по swatch для проверки picker."""
import base64
import json
import time
import urllib.parse
import urllib.request
import websocket

CDP = "http://127.0.0.1:9222"


def http(path, method="GET"):
    req = urllib.request.Request(CDP + path, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main():
    ws_url = http(f"/json/new?{urllib.parse.quote('http://127.0.0.1:8123/', safe='')}",
                  method="PUT")["webSocketDebuggerUrl"]
    ws = websocket.create_connection(ws_url, timeout=40)
    n = [0]

    def cmd(method, params=None, wait=True):
        n[0] += 1
        ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
        if not wait:
            return None
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == n[0]:
                return m.get("result", {})

    def ev(expr):
        r = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")

    for w, h, name in [(1600, 1000, "desktop"), (900, 1100, "tablet"), (430, 900, "phone")]:
        cmd("Emulation.setDeviceMetricsOverride",
            {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": w < 700})
        cmd("Page.navigate", {"url": "http://127.0.0.1:8123/"})
        time.sleep(3.5)
        for _ in range(30):
            if ev("document.querySelectorAll('.pal').length") > 0:
                break
            time.sleep(0.4)
        time.sleep(1.0)
        # раскрываем настройки чтобы попали в кадр
        if name == "desktop":
            ev("document.querySelector('#btn-settings').click()")
            time.sleep(1.2)
        shot = cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
        data = base64.b64decode(shot["data"])
        path = f"%h/ledweb/shot-{name}.png"
        with open(path, "wb") as f:
            f.write(data)
        print(f"{name} {w}x{h} -> {path} ({len(data) // 1024} КБ)")
        overflow = ev("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        print(f"   переполнение по ширине: {overflow}")
        if name == "desktop":
            ev("document.querySelector('#sheet-close').click()")
            time.sleep(0.6)
    ws.close()


if __name__ == "__main__":
    main()
