"""проверка xss: вбиваем вредное имя пресета и смотрим, выполнится ли разметка."""
import json
import time
import urllib.parse
import urllib.request
import websocket

CDP = "http://127.0.0.1:9222"
BASE = "http://127.0.0.1:8123"
# классическая полезная нагрузка: если имя выполнится — в окне появится маркер
PAYLOAD = '<img src=x onerror="window.__XSS=1">pwn'


def api(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main():
    print("создаю пресет с вредным именем...")
    try:
        res = api("/api/presets", {"name": PAYLOAD, "r": 255, "g": 0, "b": 0,
                                    "brightness": 80})
        print("  создан:", res.get("name"))
    except Exception as e:
        print("  api вернул:", e)
        return

    ws_url = None
    with urllib.request.urlopen(urllib.request.Request(
            CDP + "/json/new?" + urllib.parse.quote(BASE + "/", safe=""),
            method="PUT"), timeout=10) as r:
        ws_url = json.loads(r.read())["webSocketDebuggerUrl"]
    ws = websocket.create_connection(ws_url, timeout=40)
    n = [0]

    def ev(expr):
        n[0] += 1
        ws.send(json.dumps({"id": n[0], "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == n[0]:
                rr = m.get("result", {})
                if "exceptionDetails" in rr:
                    return "JS-ОШИБКА"
                return rr.get("result", {}).get("value")

    for _ in range(40):
        if ev("document.querySelectorAll('.pal').length") > 0:
            break
        time.sleep(0.5)
    time.sleep(1.5)

    # жмём вкладку «мои» где лежат сохранённые пресеты
    ev("""(() => {
        const t = [...document.querySelectorAll('.tab')].find(x => x.dataset.pal === 'saved');
        if (t) t.click();
    })()""")
    time.sleep(2)

    print("\nвкладка «мои» отрисовала:", ev("document.querySelectorAll('.pal').length"), "элементов")
    print("текст вредного пресета на экране:",
          ev("document.body.innerText.includes('onerror') ? 'ДА, ТЕКСТ ЕСТЬ' : 'нет'"))
    print("сработало ли onerror:", ev("window.__XSS === 1 ? 'ДА, ВЫПОЛНИЛОСЬ' : 'нет'"))
    print("есть ли <img> без обработчика:",
          ev("[...document.images].filter(i=>!i.alt&&!i.src.includes('static')).length"))
    print("img внутри .pal:", ev("document.querySelectorAll('.pal img').length"))
    print("текст первой кнопки:",
          ev("document.querySelector('.pal')?.textContent?.trim().slice(0,40)"))
    print("\nошибки js:", ev("JSON.stringify(window.__errs||[])"))

    # чистим за собой
    try:
        idx = [i for i, p in enumerate(api("/api/presets") if isinstance(api("/api/presets"), list)
               else api("/api/presets").get("presets", [])) if PAYLOAD in str(p.get("name"))]
        for i in idx:
            api(f"/api/presets?index={i}", method="DELETE")
            print("удалил пресет", i)
    except Exception as e:
        print("cleanup:", e)
    ws.close()


if __name__ == "__main__":
    main()
