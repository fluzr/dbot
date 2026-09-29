FROM python:3.12-slim

# Zainstaluj FFmpeg i certyfikaty SSL
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Kopiowanie zależności i instalacja
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiowanie kodu bota
COPY . .

# Wymuszenie bezpośredniego logowania w konsoli Dockera / Portainera
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
