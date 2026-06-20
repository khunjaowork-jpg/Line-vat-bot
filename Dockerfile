FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV TESSERACT_CMD=tesseract

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-tha \
        fonts-thai-tlwg \
        fonts-noto-core \
        fonts-noto-extra \
        fonts-noto-ui-core \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "outputs/line_expense_bot.py", "--config", "outputs/line_bot_config.render.json"]
