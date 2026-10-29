FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN useradd -u 10001 -m bridge \
    && mkdir -p /data \
    && chown bridge:bridge /data

USER bridge
ENV DB_PATH=/data/wheels-bridge.db
VOLUME /data
EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
