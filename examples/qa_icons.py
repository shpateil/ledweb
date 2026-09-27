"""считаем иконки правильно: createIcons заменяет <i data-lucide> на <svg>,
поэтому искать svg ВНУТРИ элемента с data-lucide бессмысленно.

проверяем три вещи:
  - сколько <i data-lucide> осталось незаменённых (должно быть 0 после рендера)
  - сколько svg нарисовано
  - что svg реально содержат path, а не пустые
"""
import json
import time
import urllib.request

import websocket

CDP = "http://127.0.0.1:9222"
TAB = "http://127.0.0.1:8123/"

PROBE = """
(() => {
  const left = document.querySelectorAll('i[data-lucide]').length;
  const svgs = document.querySelectorAll('svg');
  let withPath = 0;
  svgs.forEach(s => { if (s.querySelector('path, circle, line, polyline, rect')) withPath++; });
  return {
    left,
    svgs: svgs.length,
    withPath,
    sample: svgs.length ? svgs[0].outerHTML.slice(0, 90) : null,
  };
})()
"""


class Cdp:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=25)
        self.n = 10
        self.ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        while True:
            if json.loads(self.ws.recv()).get("id") == 1:
                break

    def js(self, expr):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": "Runtime.evaluate",
                                 "params": {"expression": expr, "returnByValue": True}}))
        while True:
            d = json.loads(self.ws.recv())
            if d.get("id") == self.n:
                return d.get("result", {}).get("result", {}).get("value")


def open_tab():
    tabs = json.loads(urllib.request.urlopen(f"{CDP}/json", timeout=6).read())
    for t in tabs:
        if t.get("type") == "page" and "127.0.0.1:8123" in t.get("url", ""):
            try:
                urllib.request.urlopen(f"{CDP}/json/close/{t['id']}", timeout=3)
            except Exception:
                pass
    time.sleep(0.6)
    req = urllib.request.Request(f"{CDP}/json/new?{TAB}", method="PUT")
    return json.loads(urllib.request.urlopen(req, timeout=12).read())["webSocketDebuggerUrl"]


def main():
    cdp = Cdp(open_tab())
    time.sleep(4)
    v = cdp.js(PROBE)
    print(json.dumps(v, ensure_ascii=False, indent=2))
    bad = []
    if v["left"] != 0:
        bad.append(f"осталось {v['left']} неотрисованных <i data-lucide>")
    if v["svgs"] < 10:
        bad.append(f"svg всего {v['svgs']}, ожидалось больше 10")
    if v["withPath"] != v["svgs"]:
        bad.append(f"пустых svg: {v['svgs'] - v['withPath']}")
    print("\nитог:", "всё ок" if not bad else "; ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
