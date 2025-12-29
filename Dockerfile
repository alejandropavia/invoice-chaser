FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN pip install --upgrade pip setuptools wheel \
 && pip install --only-binary=:all: -r requirements.txt

COPY . /app

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
