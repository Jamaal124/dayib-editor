FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-liberation \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

RUN fc-cache -f -v

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p uploads

EXPOSE 5000

CMD gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT app:app