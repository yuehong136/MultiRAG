FROM python:3.12-slim AS base

USER root

WORKDIR /multirag

# 安装 OpenGL 依赖、Redis、vim 和 net-tools
RUN --mount=type=cache,id=multirag_production_apt,target=/var/cache/apt,sharing=locked \
    apt update && \
    apt install -y --no-install-recommends \
        libgl1-mesa-glx \
        lsb-release \
        curl \
        gpg \
        vim \
        net-tools \
        less && \
    # 添加 Redis 的 GPG 密钥和源
    curl -fsSL https://packages.redis.io/gpg | gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg && \
    chmod 644 /usr/share/keyrings/redis-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/redis.list && \
    # 安装 Redis，使用之前的缓存
    apt update && \
    apt install -y --no-install-recommends redis && \
    # 清理 apt 缓存和安装包
    rm -rf /var/lib/apt/lists/* /var/cache/apt/* /usr/share/keyrings/redis-archive-keyring.gpg

COPY ./requirements.txt ./

# 安装 Python 依赖
RUN pip install --no-cache-dir --upgrade -r requirements.txt -i https://pypi.doubanio.com/simple
RUN pip install --no-cache-dir --upgrade transformers -i https://pypi.doubanio.com/simple

# 创建并添加 NLTK 数据
RUN mkdir -p /root/nltk_data
COPY ./nltk_data /root/nltk_data

# https://github.com/chrismattmann/tika-python
# This is the only way to run python-tika without internet access. Without this set, the default is to check the tika version and pull latest every time from Apache.
COPY tika-server-standard-3.0.0.jar tika-server-standard-3.0.0.jar.md5 ./
ENV TIKA_SERVER_JAR="file:///ragflow/tika-server-standard.jar"

# 添加其他项目文件
COPY ./api ./api
COPY ./configs ./configs
COPY ./deepdoc ./deepdoc
COPY ./core ./core
COPY ./agent ./agent
COPY ./graphrag ./graphrag
COPY ./workflow ./workflow
COPY ./errors ./errors

# 设置环境变量
ENV PYTHONPATH=/multirag/
ENV HF_ENDPOINT=https://hf-mirror.com

# 添加并配置 entrypoint
COPY ./docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# 设置 entrypoint
ENTRYPOINT ["./entrypoint.sh"]
