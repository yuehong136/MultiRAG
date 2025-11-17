# base stage
FROM ubuntu:22.04 AS base
USER root
SHELL ["/bin/bash", "-c"]

ARG LIGHTEN=0
ENV LIGHTEN=${LIGHTEN}

# 创建必要的目录
RUN mkdir -p /root/.ragdatav /root/nltk_data && \
    mkdir -p /multirag/core/res /multirag/core/res/deepdoc

# 安装libssl
RUN --mount=type=bind,from=infiniflow/ragflow_deps:latest,source=/,target=/root \
    if [ "$(uname -m)" = "x86_64" ]; then \
        dpkg -i /root/libssl1.1_1.1.1f-1ubuntu2_amd64.deb; \
    elif [ "$(uname -m)" = "aarch64" ]; then \
        dpkg -i /root/libssl1.1_1.1.1f-1ubuntu2_arm64.deb; \
    fi

#禁用交互式模式
ENV DEBIAN_FRONTEND=noninteractive

# 配置 Locale 支持中文 (必须在安装其他软件包之前)
RUN apt update && apt -y install locales && \
    locale-gen zh_CN.UTF-8 && \
    locale-gen en_US.UTF-8 && \
    update-locale LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8

# 设置环境变量: 系统级 UTF-8 编码
ENV LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh:en_US:en

# 设置apt镜像并安装依赖
RUN apt update && apt -y install ca-certificates && \
    mv /etc/apt/sources.list /etc/apt/sources.list.bak && \
    echo "deb https://mirrors.aliyun.com/ubuntu/ jammy main restricted universe multiverse" > /etc/apt/sources.list && \
    echo "deb-src https://mirrors.aliyun.com/ubuntu/ jammy main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/ubuntu/ jammy-security main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb-src https://mirrors.aliyun.com/ubuntu/ jammy-security main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/ubuntu/ jammy-updates main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb-src https://mirrors.aliyun.com/ubuntu/ jammy-updates main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/ubuntu/ jammy-backports main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb-src https://mirrors.aliyun.com/ubuntu/ jammy-backports main restricted universe multiverse" >> /etc/apt/sources.list && \
    apt update && apt install -y --no-install-recommends \
    lsb-release curl gpg libgl1-mesa-glx libdatrie-dev default-jdk vim net-tools less gcc \
    build-essential libglib2.0-0 libglx-mesa0 pkg-config libicu-dev libatk-bridge2.0-0 \
    libpython3-dev libjemalloc-dev nginx ghostscript \
    libgtk-4-1 libnss3 xdg-utils unzip libgbm-dev wget git libgdiplus  python3-pip pipx tcl-dev pkg-config \
    fonts-wqy-zenhei fonts-wqy-microhei ttf-wqy-zenhei ttf-wqy-microhei ffmpeg && \
    # 安装uv
    pip3 config set global.index-url https://mirrors.aliyun.com/pypi/simple && \
    pip3 config set global.trusted-host mirrors.aliyun.com; \
    pipx install uv -i https://mirrors.aliyun.com/pypi/simple && \
    # 安装Redis
    wget https://download.redis.io/releases/redis-7.4.3.tar.gz && \
    tar -zxvf redis-7.4.3.tar.gz && \
    cd redis-7.4.3 && \
    make && \
    make install PREFIX=/usr/local/redis && \
    mkdir -p /etc/redis  && \
    mkdir /mirror && \
    cp redis.conf /etc/redis/redis.conf && \
    cd .. && \
    rm -rf redis-7.4.3 redis-7.4.3.tar.gz && \
    cd /usr/local/redis/bin && \
    ln -s /usr/local/redis/bin/redis-server /usr/local/bin/redis-server && \
    ln -s /usr/local/redis/bin/redis-cli /usr/local/bin/redis-cli && \
    ln -s /usr/local/redis/bin/redis-benchmark /usr/local/bin/redis-benchmark && \
    sed -i 's/daemonize no/daemonize yes/' /etc/redis/redis.conf && \
    sed -i 's/# supervised no/supervised systemd/' /etc/redis/redis.conf && \
    echo "pidfile /var/run/redis.pid" >> /etc/redis/redis.conf && \
    # 清理apt缓存和安装包
    apt clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*
#将本地/mirror下的所有文件复制到容器中/mirror
COPY mirror/ /mirror/

ENV PYTHONDONTWRITEBYTECODE=1 DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
ENV PATH=/root/.local/bin:$PATH

#安装python
RUN mkdir -p /etc/uv && \
    echo 'python-install-mirror = "file:///mirror"' > /etc/uv/uv.toml && \
    uv python install 3.12.10 && \
    rm -rf /mirror  # 安装后删除

# 使用mount绑定挂载模型文件并根据条件复制到目标位置
RUN --mount=type=bind,from=infiniflow/ragflow_deps:latest,source=/huggingface.co,target=/huggingface.co \
    cp /huggingface.co/InfiniFlow/huqie/huqie.txt.trie /multirag/core/res/ && \
    tar --exclude='.*' -cf - \
        /huggingface.co/InfiniFlow/text_concat_xgb_v1.0 \
        /huggingface.co/InfiniFlow/deepdoc \
        | tar -xf - --strip-components=3 -C /multirag/core/res/deepdoc
RUN --mount=type=bind,from=infiniflow/ragflow_deps:latest,source=/huggingface.co,target=/huggingface.co \
    if [ "$LIGHTEN" != "1" ]; then \
        (tar -cf - \
            /huggingface.co/BAAI/bge-large-zh-v1.5 \
            /huggingface.co/BAAI/bge-reranker-v2-m3 \
            /huggingface.co/maidalun1020/bce-embedding-base_v1 \
            /huggingface.co/maidalun1020/bce-reranker-base_v1 \
            | tar -xf - --strip-components=2 -C /root/.ragdatav) \
    fi

# 创建并添加NLTK数据
# 设置Tika服务器
RUN --mount=type=bind,from=infiniflow/ragflow_deps:latest,source=/,target=/deps \
    cp -r /deps/nltk_data /root/ && \
    cp /deps/tika-server-standard-3.0.0.jar /deps/tika-server-standard-3.0.0.jar.md5 /multirag/ && \
    cp /deps/cl100k_base.tiktoken /multirag/9b5ad71b2ce5302211f9c61530b329a4922fc6a4

ENV TIKA_SERVER_JAR="file:///multirag/tika-server-standard-3.0.0.jar"
ENV DEBIAN_FRONTEND=noninteractive

# 添加Chrome和ChromeDriver依赖
RUN --mount=type=bind,from=infiniflow/ragflow_deps:latest,source=/chrome-linux64-121-0-6167-85,target=/chrome-linux64.zip \
    unzip /chrome-linux64.zip && \
    mv chrome-linux64 /opt/chrome && \
    ln -s /opt/chrome/chrome /usr/local/bin/
RUN --mount=type=bind,from=infiniflow/ragflow_deps:latest,source=/chromedriver-linux64-121-0-6167-85,target=/chromedriver-linux64.zip \
    unzip -j /chromedriver-linux64.zip chromedriver-linux64/chromedriver && \
    mv chromedriver /usr/local/bin/ && \
    rm -f /usr/bin/google-chrome

FROM base AS builder
USER root

WORKDIR /multirag

# install dependencies from uv.lock file
COPY pyproject.toml uv.lock ./

# https://github.com/astral-sh/uv/issues/10462
# uv records index url into uv.lock but doesn't failover among multiple indexes
RUN uv sync --python 3.12 --frozen --all-extras;

COPY .git /multirag/.git
RUN version_info=$(git describe --tags --match=v* --first-parent --always); \
    if [ "$LIGHTEN" == "1" ]; then \
        version_info="$version_info slim"; \
    else \
        version_info="$version_info full"; \
    fi; \
    echo "MultiRAG version: $version_info"; \
    echo $version_info > /multirag/VERSION

# production stage
FROM base AS production
USER root

WORKDIR /multirag

# 设置环境变量
ENV HF_ENDPOINT=https://hf-mirror.com \
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh:en_US:en \
    PYTHONIOENCODING=utf-8
ENV VIRTUAL_ENV=/multirag/.venv
COPY --from=builder ${VIRTUAL_ENV} ${VIRTUAL_ENV}
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
ENV PYTHONPATH=/multirag/

# 添加项目文件
COPY api api
COPY configs configs
COPY deepdoc deepdoc
COPY core core
COPY agent agent
COPY graphrag graphrag
COPY workflow workflow
COPY workflow_v2 workflow_v2
COPY errors errors
COPY docker docker
COPY scripts scripts
COPY admin admin
COPY pyproject.toml uv.lock alembic.ini ./
COPY agentic_reasoning agentic_reasoning
COPY mcp mcp
COPY plugin plugin

# 添加并配置entrypoint
COPY ./docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# 设置entrypoint
ENTRYPOINT ["bash", "./entrypoint.sh"]