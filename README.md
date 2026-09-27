# ledweb

панель управления ble-лентой elk-bledom. локальный демон держит соединение
с лентой постоянно, веб-морда на localhost.

```
http://127.0.0.1:8099
```

## запуск

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m ledweb.main --port 8099
```

автозапуск:

```bash
cp ledweb.service ~/.config/systemd/user/
systemctl --user enable --now ledweb
```

своя лента — флагом, mac узнать так:

```bash
bluetoothctl devices | grep -i elk
.venv/bin/python -m ledweb.main --mac AA:BB:CC:DD:EE:FF
```

## что умеет

* постоянное соединение, очередь команд, автовосстановление при обрыве
* цвет: hsv-колесо, палитры, hex, свои пресеты
* 11 своих эффектов — считаются на хосте, в прошивке их нет
* эффекты прошивки, скорость, яркость
* сцены, расписание по дням недели, таймер выключения
* горячие клавиши, тёмная тема, мобильная вёрстка
* rest + sse: состояние живёт в демоне, вкладку можно закрыть

## устройство

| | |
|---|---|
| сервис | `0000fff0-0000-1000-8000-00805f9b34fb` |
| запись | `0000fff3-0000-1000-8000-00805f9b34fb` |
| notify | `0000fff4-0000-1000-8000-00805f9b34fb` (не подписывается) |

кадр — 9 байт:

```
7E LEN CMD P1 P2 P3 P4 P5 EF
```

| команда | код | кадр |
|---|---|---|
| яркость 0-100 | `0x01` | `7e 00 01 VV ff 00 00 00 ef` |
| скорость 0-100 | `0x02` | `7e 00 02 VV 00 00 00 00 ef` |
| вкл | `0x04` | `7e 00 04 f0 00 01 ff 00 ef` |
| выкл | `0x04` | `7e 00 04 00 00 00 ff 00 ef` |
| цвет | `0x05` | `7e 00 05 03 RR GG BB 00 ef` |
| эффект | `0x03` | `7e 00 03 00 EE ff ff 00 ef` |
| время | `0x83` | `7e 00 83 00 00 00 00 00 ef` |

`LEN` в разных прошивках `0x00`, `0x04` или `0x07`. дефолт `0x00`. если
команда не применяется — переключи вариант протокола в настройках.

## ограничение

контроллер **не отдаёт состояние**. `fff4` не подписывается, по `fff3` на
чтение приходит только модель. статус в панели — кэш демона, а не опрос
железа: команда без ошибки могла не дойти. проверить можно только глазами.

## api

| путь | что |
|---|---|
| `GET /api/state` | состояние |
| `GET /api/meta` | эффекты, палитры, сцены, дни |
| `POST /api/apply` | `{"action":"color","h":270,"s":1,"v":1}` |
| `POST /api/fx` | свой эффект |
| `POST /api/fx/stop` | остановить |
| `GET POST DELETE /api/timer` | таймер |
| `POST /api/scene` | сцена по id |
| `GET POST PATCH DELETE /api/presets` | пресеты |
| `GET POST DELETE /api/rules` | расписание |
| `GET /api/scan` | поиск лент |
| `GET /api/stream` | sse |
| `GET /api/health` | живость |

## файлы

```
ledweb/
├── protocol.py   байт-коды, палитры, сцены
├── driver.py     соединение, очередь, кэш, реконнект
├── fx.py         свои эффекты
├── store.py      пресеты, правила, настройки
├── server.py     http + sse + api
├── main.py       вход
└── static/       index.html, app.css, app.js, токены, шрифты, lucide
```

веб без сборщика, сервер на стандартной библиотеке.

## тесты

```bash
chromium --headless --remote-debugging-port=9222 --remote-allow-origins='*' about:blank &
.venv/bin/python examples/qa_check.py   # вёрстка, консоль, скролл, шторка
.venv/bin/python examples/qa_live.py    # клики: цвет, сцены, эффекты, настройки
.venv/bin/python examples/test_queue.py # очередь команд без железа
.venv/bin/python examples/qa_xss.py     # экранирование имён из api
.venv/bin/python examples/test_watchdog.py # сторож: лента отвалилась -> поднимает связь
.venv/bin/python examples/qa_icons.py      # иконки отрисованы, не пустые
.venv/bin/python examples/test_linkwarn.py # панель показывает обрыв и ждёт реконнекта
```

последние два и `test_linkwarn` ждут cdp на 9222 и рвут ble по-настоящему,
так что запускать их лучше на живой ленте и не в паре с другими ble-клиентами.

## лицензия

MIT
