FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-liberation \
    wget \
    && rm -rf /var/lib/apt/lists/*

RUN wget -q "https://github.com/google/fonts/raw/main/ofl/playfairdisplay/PlayfairDisplay-Regular.ttf" \
    -O /usr/share/fonts/PlayfairDisplay-Regular.ttf

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p uploads

EXPOSE 5000

CMD python app.py