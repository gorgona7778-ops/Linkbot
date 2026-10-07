FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY main.py config.json ./
COPY assets ./assets
USER 10001
CMD ["python", "main.py"]
