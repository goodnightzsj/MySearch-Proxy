FROM python:3.11-slim
ENV MYSEARCH_PROXY_DB_PATH=/data/proxy.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# r6 C3: 容器不以 root 运行。/data 由 entrypoint 在挂载后 chown，
# 这里先建非 root 用户并把 /app /data 归属切走。
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home app \
 && mkdir -p /data \
 && chown -R app:app /app /data
USER app

EXPOSE 9874
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9874"]
