@echo off
setlocal

cd /d "%~dp0app\backend"

set PYTHONPATH=%CD%
set PUBLIC_BASE_URL=http://127.0.0.1:8000
set POOL_MIN_AVAILABLE_CODES=250000000
set POOL_SEED_BATCH_SIZE=50
set ANONYMOUS_RATE_LIMIT_PER_MINUTE=60

python -m uvicorn url_shortener.main:app --host 127.0.0.1 --port 8000
