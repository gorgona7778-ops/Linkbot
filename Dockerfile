
FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir "psycopg[binary]>=3.2,<4"

CMD ["python", "main.py"]
