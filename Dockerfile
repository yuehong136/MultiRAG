FROM mdc.datav.com/datav/multirag-base:latest AS base
USER root

WORKDIR /multirag

# install dependencies from uv.lock file
COPY pyproject.toml uv.lock ./

# https://github.com/astral-sh/uv/issues/10462
# uv records index url into uv.lock but doesn't failover among multiple indexes
RUN --mount=type=cache,id=multirag_uv,target=/root/.cache/uv,sharing=locked \
    uv sync --python 3.12 --frozen --all-extras;

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
ENV HF_ENDPOINT=https://hf-mirror.com
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
COPY pyproject.toml uv.lock ./

# 添加并配置entrypoint
COPY ./docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# 设置entrypoint
ENTRYPOINT ["bash", "./entrypoint.sh"]