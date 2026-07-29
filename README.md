# URL Shortener

## Read / Write Split

Backend поддерживает отдельные primary и replica URL для шардов:

```powershell
DATABASE_URLS=postgresql+psycopg://...primary-shard-0...,postgresql+psycopg://...primary-shard-1...
DATABASE_REPLICA_URLS=postgresql+psycopg://...replica-shard-0...,postgresql+psycopg://...replica-shard-1...
```

Запись идёт в primary-шарды. Redirect lookup, deny-list checks, analytics reads и часть чтений для кабинета используют read-side подключения.

Redirect analytics обрабатывается асинхронно через отдельный worker. В Docker Compose API публикует события, а `analytics-worker` читает их из Redis.

## Pool Defaults

Локальные Docker-дефолты намеренно облегчены:

```powershell
POOL_MIN_AVAILABLE_CODES=1000000
POOL_LOW_WATERMARK_CODES=500000
POOL_SEED_BATCH_SIZE=50000
```

Для staging/production их можно переопределить, например:

```powershell
POOL_MIN_AVAILABLE_CODES=150000000
POOL_LOW_WATERMARK_CODES=120000000
POOL_SEED_BATCH_SIZE=100000
```

## Локальный HTTPS для VK / Yandex OAuth

Для OAuth-провайдеров локально используйте HTTPS-адрес:

```text
https://localhost:8443
```

Сначала выпустите и доверьте локальный сертификат для `localhost`:

```powershell
.\scripts\setup_local_https.ps1
```

Скрипт:
- создаёт `local-certs\localhost.crt` и `local-certs\localhost.key`;
- добавляет сертификат в доверенные для текущего пользователя Windows;
- подготавливает файлы для локального HTTPS gateway в Docker.

Для OAuth-провайдеров указывайте:

```text
Base URL: https://localhost:8443
Redirect URL: https://localhost:8443/oauth/callback/<provider>
```

Примеры:

```text
https://localhost:8443/oauth/callback/yandex
https://localhost:8443/oauth/callback/vk
```

## Запуск через Docker

```powershell
docker compose up --build
```

После запуска приложение доступно:
- HTTP: `http://localhost:8000`
- HTTPS: `https://localhost:8443`
- Swagger/OpenAPI по HTTPS: `https://localhost:8443/docs`

Для браузера и OAuth используйте именно `https://localhost:8443`.

Состояние предсгенерированного пула кодов:

```powershell
Invoke-RestMethod https://localhost:8443/api/v1/pool/status
```

Если PowerShell ругается на локальный сертификат до импорта, сначала выполните `.\scripts\setup_local_https.ps1`.

## Локальный backend без Docker

```powershell
cd app/backend
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH=(Get-Location).Path
uvicorn url_shortener.main:app --reload --port 8000
```

Если нужен локальный OAuth без Docker, отдельно поднимите HTTPS reverse proxy на `https://localhost:8443` или используйте Docker HTTPS gateway из compose-файла.

## Проверки

```powershell
cd app/backend
$env:PYTHONPATH=(Get-Location).Path
pytest
```

## Нагрузочное тестирование

```powershell
pip install locust
locust -f qa/load/locustfile.py --host http://localhost:8000
```
