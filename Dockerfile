FROM python:3.12-slim

USER root
SHELL ["/bin/bash", "-c"]
ARG LIGHTEN=0
ENV LIGHTEN=${LIGHTEN}

WORKDIR /multirag

# 创建必要的目录
RUN mkdir -p /root/.ragdatav /root/nltk_data

# 复制Git信息以获取版本
COPY .git /multirag/.git
RUN version_info=$(git describe --tags --match=v* --first-parent --always); \
    if [ "$LIGHTEN" == "1" ]; then \
        version_info="$version_info slim"; \
    else \
        version_info="$version_info full"; \
    fi; \
    echo "MultiRAG version: $version_info"; \
    echo $version_info > /multirag/VERSION

# 安装libssl
COPY libssl1.1_1.1.1f-1ubuntu2_amd64.deb /root/libssl1.1_1.1.1f-1ubuntu2_amd64.deb
COPY libssl1.1_1.1.1f-1ubuntu2_arm64.deb /root/libssl1.1_1.1.1f-1ubuntu2_arm64.deb
RUN if [ "$(uname -m)" = "x86_64" ]; then \
        dpkg -i /root/libssl1.1_1.1.1f-1ubuntu2_amd64.deb; \
    elif [ "$(uname -m)" = "aarch64" ]; then \
        dpkg -i /root/libssl1.1_1.1.1f-1ubuntu2_arm64.deb; \
    fi && \
    rm -f /root/libssl1.1_*.deb

# 设置apt镜像并安装依赖
RUN echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian bookworm main" > /etc/apt/sources.list && \
    echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian-security bookworm-security main" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian bookworm-updates main" >> /etc/apt/sources.list && \
    apt update && apt install -y --no-install-recommends \
    lsb-release curl gpg libgl1-mesa-glx libdatrie-dev default-jdk vim net-tools less gcc \
    build-essential libglib2.0-0 libglx-mesa0 pkg-config libicu-dev libatk-bridge2.0-0 \
    libgtk-4-1 libnss3 xdg-utils unzip libgbm-dev wget git libgdiplus && \
    # 添加Redis的GPG密钥和源
    curl -fsSL https://packages.redis.io/gpg | gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg && \
    chmod 644 /usr/share/keyrings/redis-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/redis.list && \
    # 安装Redis
    apt update && apt install -y --no-install-recommends redis && \
    # 清理apt缓存和安装包
    apt clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/* /usr/share/keyrings/redis-archive-keyring.gpg

# 安装Python依赖
COPY ./requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip datrie -i https://pypi.doubanio.com/simple && \
    pip install --no-cache-dir --upgrade -r requirements.txt -i https://pypi.doubanio.com/simple && \
    pip install --no-cache-dir --upgrade transformers -i https://pypi.doubanio.com/simple && \
    pip install --no-cache-dir "anthropic>=0.39.0" "fasttext>=0.9.3" -i https://pypi.doubanio.com/simple && \
    rm -rf /root/.cache/pip/*

# 使用mount绑定挂载模型文件并根据条件复制到目标位置
RUN --mount=type=bind,source=./huggingface.co,target=/mnt/models \
    if [ "$LIGHTEN" != "1" ]; then \
        mkdir -p /root/.ragdatav && \
        echo "Copying large models to /root/.ragdatav..." && \
        cp -r /mnt/models/BAAI/bge-large-zh-v1.5 /root/.ragdatav/ && \
        cp -r /mnt/models/BAAI/bge-reranker-v2-m3 /root/.ragdatav/ && \
        cp -r /mnt/models/maidalun1020/bce-embedding-base_v1 /root/.ragdatav/ && \
        cp -r /mnt/models/maidalun1020/bce-reranker-base_v1 /root/.ragdatav/ && \
        echo "Large models copied successfully."; \
    else \
        echo "Lightweight mode enabled, skipping large models."; \
    fi

# 创建并添加NLTK数据
COPY ./nltk_data /root/nltk_data

# 设置Tika服务器
COPY tika-server-standard-3.0.0.jar /multirag/tika-server-standard.jar
COPY tika-server-standard-3.0.0.jar.md5 /multirag/tika-server-standard.jar.md5
ENV TIKA_SERVER_JAR="file:///multirag/tika-server-standard.jar"

# 复制tiktoken模型
COPY cl100k_base.tiktoken /multirag/9b5ad71b2ce5302211f9c61530b329a4922fc6a4

# 添加Chrome和ChromeDriver依赖
COPY chrome-linux64-121-0-6167-85 /chrome-linux64.zip
COPY chromedriver-linux64-121-0-6167-85 /chromedriver-linux64.zip
RUN unzip /chrome-linux64.zip && \
    mv chrome-linux64 /opt/chrome && \
    ln -s /opt/chrome/chrome /usr/local/bin/ && \
    unzip -j /chromedriver-linux64.zip chromedriver-linux64/chromedriver && \
    mv chromedriver /usr/local/bin/ && \
    rm -f /usr/bin/google-chrome /chrome-linux64.zip /chromedriver-linux64.zip

# 添加项目文件
COPY ./api ./api
COPY ./configs ./configs
COPY ./deepdoc ./deepdoc
COPY ./core ./core
COPY ./agent ./agent
COPY ./graphrag ./graphrag
COPY ./workflow ./workflow
COPY ./workflow_v2 ./workflow_v2
COPY ./errors ./errors
COPY ./docker ./docker

# 设置环境变量
ENV PYTHONPATH=/multirag/
ENV HF_ENDPOINT=https://hf-mirror.com
ENV DEBIAN_FRONTEND=noninteractive

# 添加并配置entrypoint
COPY ./docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# 设置entrypoint
ENTRYPOINT ["bash", "./entrypoint.sh"]