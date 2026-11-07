FROM python:3.12.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
RUN useradd --create-home --uid 10001 zhixu && mkdir -p /data && chown zhixu /data
USER zhixu
ENV ZHIXU_DATA_DIR=/data
EXPOSE 8000
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
