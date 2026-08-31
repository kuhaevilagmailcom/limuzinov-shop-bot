# LIMYZINOV SHOP

Telegram-магазин, сайт-витрина и обработчики оплат в одном Python-процессе. Отдельный сервер базы данных не нужен: по умолчанию SQLite создаётся в `data/shop.db` рядом с приложением.

## Быстрый запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app.main
```

Для Windows активация окружения: `.venv\Scripts\activate`. Минимально в `.env` нужен только `BOT_TOKEN`. Способ оплаты появляется в боте лишь тогда, когда для него заполнены ключи.

## RollyPay

Заполните `PUBLIC_BASE_URL`, `ROLLYPAY_TERMINAL_ID`, `ROLLYPAY_API_KEY` и `ROLLYPAY_SIGNING_SECRET`. Callback URL кассы:

```text
https://ваш-домен.ru/rollypay/callback
```

Сначала проверьте полный цикл с `ROLLYPAY_TEST_MODE=true`, затем включайте боевой режим. Callback проверяет HMAC-подпись, ID заказа, ID платежа, валюту и сумму.

## Админ-панель и каталог

Владелец с Telegram ID `8464597898` уже указан в коде — добавлять его в `.env` не нужно. После запуска отправьте боту `/admin`. Переменная `ADMIN_IDS` остаётся необязательной и нужна только для добавления других администраторов через запятую.

Готовых товаров в каталоге нет. В админ-панели можно создавать свои товары, менять цену СБП и цену в Stars, редактировать название/описание и скрывать позицию. Для каждого товара обязательны обе цены; покупатель выбирает СБП или Telegram Stars. Каталог хранится в `data/shop.db`, редактировать Python-файлы не нужно.

## Хостинг

Команда запуска: `python -m app.main`. Приложение слушает `0.0.0.0:8080`, отдаёт сайт на `/` и health-check на `/health`. Можно использовать готовый `Dockerfile`.

Папка `data/` должна храниться на постоянном диске. Секреты задавайте через `.env` или панель переменных окружения хостинга; `.env` исключён из Git.

## Проверка

```bash
python -m compileall -q app
python -m unittest discover -s tests -v
```
