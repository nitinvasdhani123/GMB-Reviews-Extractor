FROM mcr.microsoft.com/playwright/python:v1.53.0-jammy

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

EXPOSE 8000

CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]