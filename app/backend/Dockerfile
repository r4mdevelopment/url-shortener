FROM python:3.11-slim

WORKDIR /app/backend
ENV PYTHONPATH=/app/backend

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/url_shortener ./url_shortener
COPY frontend /app/frontend

EXPOSE 8000
CMD ["uvicorn", "url_shortener.main:app", "--host", "0.0.0.0", "--port", "8000"]
