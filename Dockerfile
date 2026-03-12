FROM python:3.12-slim

# System deps for document conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libtesseract-dev \
    poppler-utils \
    libmagic1 \
    antiword \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/docconv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY config/ ./config/

RUN mkdir -p /tmp/docconv

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

CMD ["python", "-m", "app.main"]
