# CafeBotify — START v1.0 (CLIENT)

Telegram-бот для кофейни: меню → количество → подтверждение → уведомление владельцу.
Деплой на Render Web Service (Starter), конфиг кафе хранится как Secret File.

## Что в конфиге
В Render добавьте secret file с именем `config.json` (содержимое как в config.example.json).

Поля:
- cafe.name (str)
- cafe.phone (str)
- cafe.admin_chat_id (int)
- cafe.work_hours ([startHour, endHour])
- cafe.menu (object: "Название": цена)

## Render: переменные окружения
Нужно добавить:
- BOT_TOKEN
- REDIS_URL
- WEBHOOK_SECRET (любой уникальный секрет, например cbf_xxxxx)

Render сам выставит:
- PORT
- RENDER_EXTERNAL_HOSTNAME

## Render: Secret File
Environment → Secret Files → + Add Secret File
- Filename: config.json
- Contents: вставьте JSON конфиг кафе
Save Changes (это запустит deploy)

## Проверка после деплоя
1) Откройте `https://<service>.onrender.com/` — должен вернуть JSON {"status":"healthy"...}
2) Напишите боту /start
3) Сделайте тестовый заказ → владельцу придёт уведомление

## Примечание
Меню и часы в START меняются через обновление secret file + deploy (без /admin-редактора).
