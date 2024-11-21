FROM python:3.12-slim AS base

USER root

WORKDIR /multirag

# 安装 OpenGL 依赖、Redis、vim 和 net-tools
RUN --mount=type=cache,id=multirag_production_apt,target=/var/cache/apt,sharing=locked \
    apt update && \
    apt install -y --no-install-recommends \
        libgl1-mesa-glx \
        libdatrie-dev \
        lsb-release \
        default-jdk \
        curl \
        gpg \
        vim \
        net-tools \
        less \
        gcc \
        build-essential \
        libglib2.0-0  \
        libglx-mesa0  \
        pkg-config  \
        libicu-dev  \
        libasound2t64  \
        libatk-bridge2.0-0  \
        libgtk-4-1  \
        libnss3  \
        xdg-utils  \
        unzip  \
        libgbm-dev  \
        wget \
        libgdiplus && \
    # 添加 Redis 的 GPG 密钥和源
    curl -fsSL https://packages.redis.io/gpg | gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg && \
    chmod 644 /usr/share/keyrings/redis-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/redis.list && \
    # 安装 Redis，使用之前的缓存
    apt update && \
    apt install -y --no-install-recommends redis && \
    # 清理 apt 缓存和安装包
    rm -rf /var/lib/apt/lists/* /var/cache/apt/* /usr/share/keyrings/redis-archive-keyring.gpg && \


COPY ./requirements.txt ./

# 安装 Python 依赖
RUN pip install --no-cache-dir --upgrade pip datrie && \
    pip install --no-cache-dir --upgrade -r requirements.txt -i https://pypi.doubanio.com/simple
    pip install --no-cache-dir --upgrade transformers -i https://pypi.doubanio.com/simple && \
    pip install --no-cache-dir  anthropic >= 0.39.0 fasttext >= 0.9.3

# 创建并添加 NLTK 数据
RUN mkdir -p /root/nltk_data
COPY ./nltk_data /root/nltk_data

# https://github.com/chrismattmann/tika-python
# This is the only way to run python-tika without internet access. Without this set, the default is to check the tika version and pull latest every time from Apache.
COPY tika-server-standard-3.0.0.jar /multirag/tika-server-standard.jar
COPY tika-server-standard-3.0.0.jar.md5 /multirag/tika-server-standard.jar.md5
ENV TIKA_SERVER_JAR="file:///multirag/tika-server-standard.jar"

# Copy cl100k_base
COPY cl100k_base.tiktoken /ragflow/9b5ad71b2ce5302211f9c61530b329a4922fc6a4

# Add dependencies of selenium
RUN --mount=type=bind,source=chrome-linux64-121-0-6167-85,target=/chrome-linux64.zip \
    unzip /chrome-linux64.zip && \
    mv chrome-linux64 /opt/chrome/ && \
    ln -s /opt/chrome/chrome /usr/local/bin/
RUN --mount=type=bind,source=chromedriver-linux64-121-0-6167-85,target=/chromedriver-linux64.zip \
    unzip -j /chromedriver-linux64.zip chromedriver-linux64/chromedriver && \
    mv chromedriver /usr/local/bin/ && \
    rm -f /usr/bin/google-chrome

# 添加其他项目文件
COPY ./api ./api
COPY ./configs ./configs
COPY ./deepdoc ./deepdoc
COPY ./core ./core
COPY ./agent ./agent
COPY ./graphrag ./graphrag
COPY ./workflow ./workflow
COPY ./errors ./errors
ADD ./docker ./docker

# 设置环境变量
ENV PYTHONPATH=/multirag/
ENV HF_ENDPOINT=https://hf-mirror.com

# 添加并配置 entrypoint
ADD docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# 设置 entrypoint
ENTRYPOINT ["bash", "./entrypoint.sh"]
