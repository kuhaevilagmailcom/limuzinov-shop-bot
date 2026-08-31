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

## Crypto Pay

Добавьте `CRYPTOPAY_TOKEN` и укажите webhook:

```text
https://ваш-домен.ru/webhooks/cryptopay
```

Для тестов используйте `https://testnet-pay.crypt.bot`.

## Админ-панель и каталог

1. Отправьте боту `/id`.
2. Запишите полученный номер в `ADMIN_IDS` файла `.env`.
3. Перезапустите приложение и откройте `/admin`.

В админ-панели можно создавать товары, менять рублёвую цену и цену в Stars, редактировать название/описание и скрывать позицию из каталога. Каталог хранится в `data/shop.db`; редактировать Python-файлы не нужно.

«Создать песню» находится в общем каталоге как цифровой товар. Перед оплатой бот собирает техническое задание. Stars включаются для любого товара, которому в админке задана цена в ⭐.

## Хостинг

Команда запуска: `python -m app.main`. Приложение слушает `0.0.0.0:8080`, отдаёт сайт на `/` и health-check на `/health`. Можно использовать готовый `Dockerfile`.

Папка `data/` должна храниться на постоянном диске. Секреты задавайте через `.env` или панель переменных окружения хостинга; `.env` исключён из Git.

## Проверка

```bash
python -m compileall -q app
python -m unittest discover -s tests -v
```
