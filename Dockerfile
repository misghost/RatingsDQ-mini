# 微信云托管部署镜像（Flask + gunicorn）
FROM python:3.11-slim

WORKDIR /app

# 优先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY . .

# 云托管会注入 PORT（默认 80）。RATING_DB 默认指向挂载卷 /data，
# 控制台「存储挂载」把文件存储(CFS)挂到 /data 即可持久化、多实例共享。
ENV RATING_DB=/data/rating.db \
    PORT=80

EXPOSE 80

# 单 worker + gthread 多线程：配合 SQLite WAL，避免多进程写锁竞争。
# 需要更高并发时，建议改用云数据库(MySQL/Postgres)并提升 --workers。
CMD ["sh", "-c", "gunicorn -w 1 -k gthread --threads 8 -b 0.0.0.0:${PORT:-80} --timeout 120 server:app"]
