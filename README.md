# linux-tg-yt-proxy

YouTube и Telegram для всей домашней сети через один маленький Linux-сервер. **Без VPN**: трафик идёт напрямую, сервер только мешает DPI провайдера.

| Что | Порт | Как работает |
|---|---|---|
| YouTube | `1080` | Прокси HTTP + SOCKS5 + PAC-файл. Режет TLS ClientHello на куски, чтобы DPI не видел имя сайта (как byedpi). |
| Telegram | `1443` | MTProto-прокси [Flowseal/tg-ws-proxy](https://github.com/Flowseal/tg-ws-proxy): Telegram через WebSocket, заблокированные дата-центры через Cloudflare. |
| Боты | — | Если провайдер режет IP `api.telegram.org`, установщик закрепляет рабочий IP в `/etc/hosts`. |

## Установка одной командой

Нужен Debian 12/13 или Ubuntu 22.04+ с systemd, root, сервер в домашней сети. Хватает 1 ядра и 512 МБ.

```bash
curl -fsSL https://raw.githubusercontent.com/Aut0iq/linux-tg-yt-proxy/main/install.sh | sudo bash
```

Пока репозиторий закрытый, нужен GitHub-токен с доступом к нему (`gh auth token` или Personal access token):

```bash
T=ваш_токен; curl -fsSL -H "Authorization: Bearer $T" https://raw.githubusercontent.com/Aut0iq/linux-tg-yt-proxy/main/install.sh | sudo GH_TOKEN=$T bash
```

Или из клона: `git clone https://github.com/Aut0iq/linux-tg-yt-proxy && sudo bash linux-tg-yt-proxy/install.sh`.

Установка занимает 2–5 минут. Установщик сам:

1. ставит пакеты (`python3`, `python3-venv`, `curl`);
2. **подбирает способ обхода для YouTube**: пробует `none`, `split`, `disorder` по 12 запросов и берёт лучший (при равенстве самый простой);
3. **подбирает IP серверов Telegram** DC1–DC5; заблокированные пойдут через Cloudflare;
4. **проверяет `api.telegram.org`** и при необходимости закрепляет рабочий IP (с учётом cloud-init);
5. ставит службы `yt-proxy` и `tg-ws-proxy` с автозапуском;
6. **проверяет всё в конце**: YouTube через прокси, Bot API и MTProto-рукопожатие с каждым DC. Печатает ссылки для подключения.

Повторный запуск подбирает всё заново. Порты и секрет Telegram сохраняются в `/etc/linux-tg-yt-proxy.conf`, так что клиентов перенастраивать не нужно.

## Подключение устройств

**Telegram**: откройте на телефоне или ПК ссылку `tg://proxy?...` из вывода установщика. Вручную: тип MTProto, сервер — IP сервера, порт `1443`, секрет `dd…` из ссылки.

**YouTube**: включите на устройстве автонастройку прокси (PAC) с адресом `http://IP_СЕРВЕРА:1080/proxy.pac`. Через сервер пойдёт только YouTube, остальное напрямую.

- Windows: Параметры → Сеть и Интернет → Прокси → Использовать сценарий настройки.
- Android: Wi-Fi → сеть → Прокси → Автоматически.
- iPhone: Wi-Fi → (i) → Настройка прокси → Авто → URL.

Можно и без PAC: прокси HTTP или SOCKS5 `IP_СЕРВЕРА:1080`.

**Боты** на этом сервере: укажите в боте прокси `http://127.0.0.1:1080` и используйте long polling (для webhook нужен внешний IP).

- aiogram 3: `Bot(token, session=AiohttpSession(proxy="http://127.0.0.1:1080"))`
- python-telegram-bot: `Application.builder().token(token).proxy("http://127.0.0.1:1080").get_updates_proxy("http://127.0.0.1:1080")`

Без прокси бот тоже будет работать, но провайдер обрывает часть новых соединений к Telegram, и ответы иногда будут приходить на несколько секунд позже. Прокси запускает новую попытку каждые 0,5 с и берёт первую удавшуюся, как браузеры (Happy Eyeballs).

## Управление

```bash
systemctl status yt-proxy tg-ws-proxy      # состояние
journalctl -u tg-ws-proxy -f               # журнал Telegram-прокси
sudo bash install.sh                       # подобрать настройки заново
```

Удаление:

```bash
sudo systemctl disable --now yt-proxy tg-ws-proxy
sudo rm -rf /opt/linux-tg-yt-proxy /etc/linux-tg-yt-proxy.conf /etc/systemd/system/{yt-proxy,tg-ws-proxy}.service
sudo sed -i '/# linux-tg-yt-proxy$/d' /etc/hosts /etc/cloud/templates/hosts.*.tmpl 2>/dev/null
```

## Безопасность

- Прокси YouTube принимает соединения только из частных сетей (192.168.x, 10.x, 172.16–31.x).
- Telegram-прокси защищён секретом.
- Не пробрасывайте порты 1080 и 1443 в интернет.

## Файлы

- `install.sh`: установщик, автоподбор и финальная проверка.
- `yt-proxy.py`: прокси для YouTube, только стандартная библиотека Python. При запуске проверяет сам себя.
- `tg-check.py`: проверка Telegram-прокси, как у настоящего клиента: запрос `req_pq` к каждому DC.
- tg-ws-proxy скачивается при установке, версия закреплена на проверенном коммите. Другую можно взять так: `TG_WS_PROXY_REF=<commit> sudo bash install.sh`.
