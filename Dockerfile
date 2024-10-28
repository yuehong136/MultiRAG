FROM python:3.12

USER root

WORKDIR /multirag

# 安装 OpenGL 依赖、Redis、vim 和 net-tools
RUN apt-get update && \
    apt-get install -y libgl1-mesa-glx lsb-release curl gpg vim net-tools && \
    curl -fsSL https://packages.redis.io/gpg | gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg && \
    chmod 644 /usr/share/keyrings/redis-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/redis.list && \
    apt-get update && \
    apt-get install -y redis

ADD ./requirements.txt ./

# 安装 Python 依赖
RUN pip install --no-cache-dir --upgrade -r requirements.txt -i https://pypi.doubanio.com/simple
RUN pip install --no-cache-dir --upgrade transformers -i https://pypi.doubanio.com/simple

# 创建并添加 NLTK 数据
RUN mkdir -p /root/nltk_data
ADD ./nltk_data /root/nltk_data

# 添加其他项目文件
ADD ./api ./api
ADD ./configs ./configs
ADD ./deepdoc ./deepdoc
ADD ./core ./core
ADD ./agent ./agent
ADD ./graphrag ./graphrag
ADD ./workflow ./workflow
ADD ./errors ./errors

# 设置环境变量
ENV PYTHONPATH=/multirag/
ENV HF_ENDPOINT=https://hf-mirror.com

# 添加并配置 entrypoint
ADD ./docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# 设置 entrypoint
ENTRYPOINT ["./entrypoint.sh"]
