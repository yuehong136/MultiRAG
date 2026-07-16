ARG MULTIRAG_DEPS_IMAGE=multirag_deps:uv0.11.27-tika3.3.0-build-only
FROM ${MULTIRAG_DEPS_IMAGE} AS multirag_deps

# base stage
FROM ubuntu:24.04 AS base
USER root
SHELL ["/bin/bash", "-c"]

ARG NEED_MIRROR=0
ARG MULTIRAG_DEPS_IMAGE
ARG UV_VERSION=0.11.27

# 创建必要的目录
RUN mkdir -p /root/.ragdatav /root/nltk_data && \
    mkdir -p /multirag/core/res /multirag/core/res/deepdoc

# 安装libssl
RUN --mount=type=bind,from=multirag_deps,source=/,target=/root \
    if [ "$(uname -m)" = "x86_64" ]; then \
        dpkg -i /root/libssl1.1_1.1.1f-1ubuntu2_amd64.deb; \
    elif [ "$(uname -m)" = "aarch64" ]; then \
        dpkg -i /root/libssl1.1_1.1.1f-1ubuntu2_arm64.deb; \
    fi

#禁用交互式模式
ENV DEBIAN_FRONTEND=noninteractive

# 配置 Locale 支持中文 (必须在安装其他软件包之前)
RUN if [ "$NEED_MIRROR" == "1" ]; then \
        # ca-certificates 尚未安装，先使用 HTTP 镜像，下一步安装 CA 后再切换 HTTPS。
        sed -i 's|http://archive.ubuntu.com/ubuntu|http://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list.d/ubuntu.sources; \
        sed -i 's|http://security.ubuntu.com/ubuntu|http://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list.d/ubuntu.sources; \
    fi && \
    apt update && apt -y install locales && \
    locale-gen zh_CN.UTF-8 && \
    locale-gen en_US.UTF-8 && \
    update-locale LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8

# 设置环境变量: 系统级 UTF-8 编码
ENV LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh:en_US:en

# 设置apt镜像并安装依赖
# Python package and implicit dependencies:
# opencv-python: libglib2.0-0 libglx-mesa0 libgl1
# python-pptx:   default-jdk
# selenium:      libatk-bridge2.0-0
# Building C extensions: libpython3-dev libgtk-4-1 libnss3 xdg-utils libgbm-dev
RUN --mount=type=cache,id=multirag_apt,target=/var/cache/apt,sharing=locked \
    apt update && apt -y install ca-certificates && \
    if [ "$NEED_MIRROR" == "1" ]; then \
        sed -i 's|http://archive.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list.d/ubuntu.sources; \
        sed -i 's|http://security.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list.d/ubuntu.sources; \
        sed -i 's|http://mirrors.aliyun.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list.d/ubuntu.sources; \
    fi; \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache && \
    apt update && \
    apt install -y libglib2.0-0 libglx-mesa0 libgl1 && \
    apt install -y pkg-config libicu-dev libgdiplus && \
    apt install -y default-jdk && \
    apt install -y libatk-bridge2.0-0 && \
    apt install -y libpython3-dev libgtk-4-1 libnss3 xdg-utils libgbm-dev && \
    apt install -y libjemalloc-dev && \
    apt install -y gnupg unzip curl wget git vim less && \
    apt install -y ghostscript && \
    apt install -y pandoc && \
    apt install -y texlive texlive-latex-extra texlive-xetex texlive-lang-chinese && \
    apt install -y fonts-freefont-ttf fonts-noto-cjk && \
    apt install -y postgresql-client && \
    apt install -y fonts-wqy-zenhei fonts-wqy-microhei && \
    apt install -y lsb-release build-essential gcc libdatrie-dev net-tools tcl-dev ffmpeg

# Download resource from GitHub to /usr/share/infinity
RUN mkdir -p /usr/share/infinity/resource && \
    if [ "$NEED_MIRROR" == "1" ]; then \
        git clone --depth 1 --single-branch https://gitee.com/infiniflow/resource /tmp/resource; \
    else \
        git clone --depth 1 --single-branch https://github.com/infiniflow/resource.git /tmp/resource; \
    fi && \
    cp -r /tmp/resource/* /usr/share/infinity/resource && \
    rm -rf /tmp/resource

ARG NGINX_VERSION=1.29.5-1~noble
RUN --mount=type=cache,id=multirag_apt,target=/var/cache/apt,sharing=locked \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://nginx.org/keys/nginx_signing.key | gpg --dearmor -o /etc/apt/keyrings/nginx-archive-keyring.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nginx-archive-keyring.gpg] https://nginx.org/packages/mainline/ubuntu/ noble nginx" > /etc/apt/sources.list.d/nginx.list && \
    apt update && \
    apt install -y nginx=${NGINX_VERSION} && \
    apt-mark hold nginx

# 安装 MSSQL ODBC 驱动 (pyodbc 依赖)
# macOS ARM64 环境安装 msodbcsql18，x86_64 环境安装 msodbcsql17
# 使用 Ubuntu 22.04 仓库，因为它同时包含 msodbcsql17 和 msodbcsql18
# 注意：msodbcsql17 不支持 Ubuntu 24.04，所以需要用 22.04 的仓库
RUN --mount=type=cache,id=multirag_apt,target=/var/cache/apt,sharing=locked \
    curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - && \
    curl https://packages.microsoft.com/config/ubuntu/22.04/prod.list > /etc/apt/sources.list.d/mssql-release.list && \
    apt update && \
    arch="$(uname -m)"; \
    if [ "$arch" = "arm64" ] || [ "$arch" = "aarch64" ]; then \
        ACCEPT_EULA=Y apt install -y unixodbc-dev msodbcsql18; \
    else \
        ACCEPT_EULA=Y apt install -y unixodbc-dev msodbcsql17; \
    fi || \
    { echo "Failed to install ODBC driver"; exit 1; }

# 安装Redis (从源码编译)
ARG REDIS_BUILD_JOBS=2
RUN wget https://download.redis.io/releases/redis-7.4.3.tar.gz && \
    tar -zxvf redis-7.4.3.tar.gz && \
    cd redis-7.4.3 && \
    make -j"${REDIS_BUILD_JOBS}" OPT="-O2" && \
    make install PREFIX=/usr/local/redis && \
    mkdir -p /etc/redis /mirror && \
    cp redis.conf /etc/redis/redis.conf && \
    cd .. && \
    rm -rf redis-7.4.3 redis-7.4.3.tar.gz && \
    ln -s /usr/local/redis/bin/redis-server /usr/local/bin/redis-server && \
    ln -s /usr/local/redis/bin/redis-cli /usr/local/bin/redis-cli && \
    ln -s /usr/local/redis/bin/redis-benchmark /usr/local/bin/redis-benchmark && \
    sed -i 's/daemonize no/daemonize yes/' /etc/redis/redis.conf && \
    sed -i 's/# supervised no/supervised systemd/' /etc/redis/redis.conf && \
    echo "pidfile /var/run/redis.pid" >> /etc/redis/redis.conf && \
    # 清理apt缓存
    apt clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*
#将本地/mirror下的所有文件复制到容器中/mirror
COPY mirror/ /mirror/

ENV PYTHONDONTWRITEBYTECODE=1 DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
ENV PATH=/root/.local/bin:$PATH

# 安装uv并配置镜像源
RUN --mount=type=bind,from=multirag_deps,source=/,target=/deps \
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
    else \
        echo "Unsupported architecture for uv: $arch" >&2; \
        exit 1; \
    fi; \
    actual_uv_version="$(uv --version | awk '{print $2}')"; \
    if [ "$actual_uv_version" != "$UV_VERSION" ]; then \
        echo "Expected uv $UV_VERSION from $MULTIRAG_DEPS_IMAGE, got $actual_uv_version" >&2; \
        exit 1; \
    fi; \
    uv python install 3.12.10 && \
    rm -rf /mirror

# 只复制镜像构建期必需的 DeepDoc 资源。运行时 embedding/rerank
# 模型不再进入镜像，由宿主机目录挂载到固定路径 /root/.ragdatav。
RUN --mount=type=bind,from=multirag_deps,source=/huggingface.co,target=/huggingface.co \
    tar --exclude='.*' -cf - \
        /huggingface.co/InfiniFlow/text_concat_xgb_v1.0 \
        /huggingface.co/InfiniFlow/deepdoc \
        | tar -xf - --strip-components=3 -C /multirag/core/res/deepdoc

# 创建并添加NLTK数据
# 设置Tika服务器
RUN --mount=type=bind,from=multirag_deps,source=/,target=/deps \
    cp -r /deps/nltk_data /root/ && \
    cp /deps/tika-server-standard-3.3.0.jar /deps/tika-server-standard-3.3.0.jar.md5 /multirag/ && \
    cp /deps/cl100k_base.tiktoken /multirag/9b5ad71b2ce5302211f9c61530b329a4922fc6a4

ENV TIKA_SERVER_JAR="file:///multirag/tika-server-standard-3.3.0.jar"
ENV DEBIAN_FRONTEND=noninteractive

# 添加Chrome和ChromeDriver依赖
RUN --mount=type=bind,from=multirag_deps,source=/chrome-linux64-121-0-6167-85,target=/chrome-linux64.zip \
    unzip /chrome-linux64.zip && \
    mv chrome-linux64 /opt/chrome && \
    ln -s /opt/chrome/chrome /usr/local/bin/
RUN --mount=type=bind,from=multirag_deps,source=/chromedriver-linux64-121-0-6167-85,target=/chromedriver-linux64.zip \
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
    uv sync --python 3.12 --frozen --all-extras && \
    # Ensure pip is available in the venv for runtime package installation (fixes #12651)
    .venv/bin/python3 -m ensurepip --upgrade

COPY .git /multirag/.git
RUN version_info=$(git describe --tags --match=v* --first-parent --always); \
    version_info="$version_info"; \
    echo "MultiRAG version: $version_info"; \
    echo $version_info > /multirag/VERSION

# production stage
FROM base AS production
USER root

WORKDIR /multirag

LABEL io.multirag.runtime-models="external" \
      io.multirag.runtime-model-path="/root/.ragdatav"

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
COPY workflow workflow
COPY workflow_v2 workflow_v2
COPY errors errors
COPY docker docker
COPY scripts scripts

# Nginx 配置打进镜像（不再依赖 docker-compose 挂载）
# 站点配置提供三套变体，由 entrypoint.sh 按 API_PROXY_SCHEME 运行时选择
COPY docker/nginx/nginx.conf /etc/nginx/nginx.conf
COPY docker/nginx/proxy.conf /etc/nginx/proxy.conf
COPY docker/nginx/multirag.conf.python /etc/nginx/conf.d/multirag.conf.python
COPY docker/nginx/multirag.conf.golang /etc/nginx/conf.d/multirag.conf.golang
COPY docker/nginx/multirag.conf.hybrid /etc/nginx/conf.d/multirag.conf.hybrid
RUN rm -f /etc/nginx/conf.d/default.conf /etc/nginx/sites-enabled/default 2>/dev/null || true

COPY admin admin
COPY pyproject.toml uv.lock alembic.ini ./
COPY mcp mcp
COPY common common
COPY memory memory
COPY bin bin

# 添加并配置entrypoint
COPY ./docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# 设置entrypoint
ENTRYPOINT ["bash", "./entrypoint.sh"]
