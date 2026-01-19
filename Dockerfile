# base stage
FROM ubuntu:24.04 AS base
USER root
SHELL ["/bin/bash", "-c"]

ARG NEED_MIRROR=0

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
    if [ "$NEED_MIRROR" == "1" ]; then \
        sed -i 's|http://archive.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list.d/ubuntu.sources; \
        sed -i 's|http://security.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list.d/ubuntu.sources; \
    fi; \
    apt update && apt install -y --no-install-recommends \
    lsb-release curl gpg libgl1-mesa-glx libdatrie-dev default-jdk vim net-tools less gcc \
    build-essential libglib2.0-0 libglx-mesa0 pkg-config libicu-dev libatk-bridge2.0-0 \
    libpython3-dev libjemalloc-dev nginx ghostscript \
    libgtk-4-1 libnss3 xdg-utils unzip libgbm-dev wget git libgdiplus tcl-dev pkg-config \
    fonts-wqy-zenhei fonts-wqy-microhei ttf-wqy-zenhei ttf-wqy-microhei ffmpeg && \
    # 安装 MSSQL ODBC 驱动 (pyodbc 依赖)
    curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - && \
    curl https://packages.microsoft.com/config/ubuntu/24.04/prod.list > /etc/apt/sources.list.d/mssql-release.list && \
    apt update && \
    arch="$(uname -m)"; \
    if [ "$arch" = "arm64" ] || [ "$arch" = "aarch64" ]; then \
        ACCEPT_EULA=Y apt install -y unixodbc-dev msodbcsql18; \
    else \
        ACCEPT_EULA=Y apt install -y unixodbc-dev msodbcsql17; \
    fi && \
    fonts-wqy-zenhei fonts-wqy-microhei ttf-wqy-zenhei ttf-wqy-microhei ffmpeg \
    pandoc texlive fonts-freefont-ttf fonts-noto-cjk && \
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

# 安装uv并配置镜像源
RUN --mount=type=bind,from=infiniflow/ragflow_deps:latest,source=/,target=/deps \
    mkdir -p /etc/uv && \
    echo 'python-install-mirror = "file:///mirror"' > /etc/uv/uv.toml && \
    if [ "$NEED_MIRROR" == "1" ]; then \
        echo '[[index]]' >> /etc/uv/uv.toml && \
        echo 'url = "https://pypi.tuna.tsinghua.edu.cn/simple"' >> /etc/uv/uv.toml && \
        echo 'default = true' >> /etc/uv/uv.toml; \
    fi; \
    arch="$(uname -m)"; \
    if [ "$arch" = "x86_64" ]; then \
        tar xzf /deps/uv-x86_64-unknown-linux-gnu.tar.gz && \
        cp uv-x86_64-unknown-linux-gnu/* /usr/local/bin/ && \
        rm -rf uv-x86_64-unknown-linux-gnu; \
    elif [ "$arch" = "aarch64" ]; then \
        tar xzf /deps/uv-aarch64-unknown-linux-gnu.tar.gz && \
        cp uv-aarch64-unknown-linux-gnu/* /usr/local/bin/ && \
        rm -rf uv-aarch64-unknown-linux-gnu; \
    fi && \
    uv python install 3.12.10 && \
    rm -rf /mirror

# 使用mount绑定挂载模型文件并根据条件复制到目标位置
RUN --mount=type=bind,from=infiniflow/ragflow_deps:latest,source=/huggingface.co,target=/huggingface.co \
    tar --exclude='.*' -cf - \
        /huggingface.co/InfiniFlow/text_concat_xgb_v1.0 \
        /huggingface.co/InfiniFlow/deepdoc \
        | tar -xf - --strip-components=3 -C /multirag/core/res/deepdoc
RUN --mount=type=bind,from=infiniflow/ragflow_deps:latest,source=/huggingface.co,target=/huggingface.co \
    tar -cf - \
        /huggingface.co/BAAI/bge-large-zh-v1.5 \
        /huggingface.co/BAAI/bge-reranker-v2-m3 \
        /huggingface.co/maidalun1020/bce-embedding-base_v1 \
        | tar -xf - --strip-components=2 -C /root/.ragdatav

# 创建并添加NLTK数据
# 设置Tika服务器
RUN --mount=type=bind,from=infiniflow/ragflow_deps:latest,source=/,target=/deps \
    cp -r /deps/nltk_data /root/ && \
    cp /deps/tika-server-standard-3.2.3.jar /deps/tika-server-standard-3.2.3.jar.md5 /multirag/ && \
    cp /deps/cl100k_base.tiktoken /multirag/9b5ad71b2ce5302211f9c61530b329a4922fc6a4

ENV TIKA_SERVER_JAR="file:///multirag/tika-server-standard-3.2.3.jar"
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
RUN --mount=type=cache,id=multirag_uv,target=/root/.cache/uv,sharing=locked \
    if [ "$NEED_MIRROR" == "1" ]; then \
        sed -i 's|pypi.org|pypi.tuna.tsinghua.edu.cn|g' uv.lock; \
    else \
        sed -i 's|pypi.tuna.tsinghua.edu.cn|pypi.org|g' uv.lock; \
    fi; \
    uv sync --python 3.12 --frozen --all-extras

COPY .git /multirag/.git
RUN version_info=$(git describe --tags --match=v* --first-parent --always); \
    version_info="$version_info"; \
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
COPY common common

# 添加并配置entrypoint
COPY ./docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# 设置entrypoint
ENTRYPOINT ["bash", "./entrypoint.sh"]