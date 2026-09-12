# 纯 Zeabur 一体化服务（静态站 + 定时刷新）
# 构建上下文 = 仓库根目录，因此 COPY 均以仓库根目录相对路径书写
FROM python:3.11-slim

# cartopy 需要 geos/proj；fonts-noto-cjk 用于中文标注
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgeos-dev \
        libproj-dev \
        proj-bin \
        gcc \
        g++ \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# 先装依赖层，善用缓存
COPY zeabur/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码：Web 服务 + 生成脚本 + 静态站点
COPY zeabur/app.py ./app.py
COPY scripts/ ./scripts/
COPY index.html ./web/index.html

EXPOSE 8080
CMD ["python", "app.py"]