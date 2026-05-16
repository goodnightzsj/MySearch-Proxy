FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt /tmp/mysearch-requirements.txt
RUN pip install --no-cache-dir -r /tmp/mysearch-requirements.txt

COPY . /app/mysearch
RUN chmod +x /app/mysearch/docker-entrypoint.sh

# r6 C3: 非 root 用户运行
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home app \
 && chown -R app:app /app
USER app

EXPOSE 8000

ENTRYPOINT ["/app/mysearch/docker-entrypoint.sh"]
CMD ["python", "-m", "mysearch", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
