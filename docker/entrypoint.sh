#!/usr/bin/env bash

###############################################################################
# MultiRAG 容器入口脚本（增强版）                                             #
# --------------------------------------------------------------------------- #
# 主要特性                                                                    #
#   1. 通过命令行参数灵活启停 Redis、API Server 与 TaskExecutor 组件          #
#   2. 自动生成 ≤32 字符的 HOST_ID，若超长则取主机名 MD5，保证唯一稳定         #
#   3. **保留默认宽容模式**：脚本不会因单个命令失败而直接退出（可通过         #
#      环境变量 STRICT_MODE=1 再显式开启 "set -euo pipefail"）                #
#   4. TaskExecutor 进程使用 jemalloc 预加载，崩溃后 1 秒内自动重启           #
#   5. 捕获 SIGTERM/SIGINT，实现 Docker/K8s 优雅关停                          #
#   6. 兼容 WS 环境变量，也支持 --workers / --consumer-no-beg/end 精细控制     #
###############################################################################

# --------------------------- 可选严格模式 -----------------------------------
# 若需要在 CI/CD 等环境保证启动脚本一旦出错立即失败，可在 docker run 时
# 传入 "-e STRICT_MODE=1"，脚本将启用 "set -euo pipefail"。
if [[ "${STRICT_MODE:-0}" -eq 1 ]]; then
  set -euo pipefail
fi

# --------------------------- 默认参数 ---------------------------------------
PY=${PYTHON_BIN:-python3}       # Python 可执行文件，可通过环境变量 PYTHON_BIN 覆盖
ENABLE_REDIS=1                 # 是否启动 Redis
ENABLE_SERVER=1                # 是否启动 api.multirag_server
ENABLE_TASKEXECUTOR=1          # 是否启动 TaskExecutor
ENABLE_DATASYNC=1              # 是否启动数据源同步服务
ENABLE_ADMINSERVER=1           # 是否启动 admin server（默认启动）
ENABLE_WEBSERVER="${ENABLE_WEBSERVER:-1}"  # 是否启动 Nginx（默认启动）
WORKERS="${WS:-1}"            # TaskExecutor 数量，默认取 WS 环境变量，否则 1
CONSUMER_NO_BEG=0              # 消费者 ID 起始（含）
CONSUMER_NO_END=0              # 消费者 ID 结束（不含） - 为 0 表示未指定区间
REDIS_CONF="${REDIS_CONF_PATH:-/etc/redis/redis.conf}"   # Redis 配置文件路径
LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu/"             # jemalloc 依赖
export LD_LIBRARY_PATH

# --------------------------- HOST_ID 逻辑 -----------------------------------
_current_hostname="$(hostname)"
if [[ ${#_current_hostname} -le 32 ]]; then
  HOST_ID="${_current_hostname}"
else
  HOST_ID="$(printf "%s" "${_current_hostname}" | md5sum | awk '{print $1}')"
fi

# --------------------------- 帮助信息 ---------------------------------------
usage() {
  cat <<EOF
Usage: $0 [options]

  --disable-redis                 不启动 Redis
  --disable-server                不启动 api.multirag_server
  --disable-taskexecutor          不启动 TaskExecutor
  --disable-datasync              不启动数据源同步服务
  --disable-webserver             不启动 Nginx（Web Server）
  --enable-adminserver            启动 Admin Server（默认启动）
  --workers=<num>                 TaskExecutor 数量（默认读取 WS 或 1）
  --consumer-no-beg=<num>         消费者 ID 起始（含）
  --consumer-no-end=<num>         消费者 ID 结束（不含）
  --host-id=<string>              手动指定 HOST_ID
  -h | --help                     显示此帮助
EOF
  exit 0
}

# --------------------------- 解析命令行参数 ---------------------------------
for arg in "$@"; do
  case $arg in
    --disable-redis)        ENABLE_REDIS=0 ; shift ;;
    --disable-server)       ENABLE_SERVER=0 ; shift ;;
    --disable-taskexecutor) ENABLE_TASKEXECUTOR=0 ; shift ;;
    --disable-datasync)     ENABLE_DATASYNC=0 ; shift ;;
    --disable-webserver)    ENABLE_WEBSERVER=0 ; shift ;;
    --enable-adminserver)   ENABLE_ADMINSERVER=1 ; shift ;;
    --workers=*)            WORKERS="${arg#*=}" ; shift ;;
    --consumer-no-beg=*)    CONSUMER_NO_BEG="${arg#*=}" ; shift ;;
    --consumer-no-end=*)    CONSUMER_NO_END="${arg#*=}" ; shift ;;
    --host-id=*)            HOST_ID="${arg#*=}" ; shift ;;
    -h|--help)              usage ;;
    *)                      echo "未知参数: $arg" ; usage ;;
  esac
done

# --------------------------- 函数定义 ---------------------------------------
start_nginx() {
  echo "[entrypoint] 启动 Nginx..."
  /usr/sbin/nginx
}

start_redis() {
  echo "[entrypoint] 启动 Redis -> ${REDIS_CONF}"
  redis-server "${REDIS_CONF}" &
}

function task_exe() {
  local cid="$1"
  local hid="$2"
  local jemalloc="$(pkg-config --variable=libdir jemalloc)/libjemalloc.so"
  echo "[entrypoint] TaskExecutor ${hid}_${cid} 启动..."
  while true; do
    LD_PRELOAD="${jemalloc}" "${PY}" -m core.svr.task_executor "${hid}_${cid}" || true
    echo "[entrypoint] TaskExecutor ${hid}_${cid} 崩溃，1 秒后重启..."
    sleep 1
  done &
}

start_server() {
  echo "[entrypoint] 启动 api.multirag_server..."
  exec "${PY}" -m api.multirag_server
}

start_admin_server() {
  echo "[entrypoint] 启动 Admin Server (端口 8130)..."
  while true; do
    "${PY}" admin/server/admin_server.py || true
    echo "[entrypoint] Admin Server 崩溃，1 秒后重启..."
    sleep 1
  done &
}

ensure_docling() {
  [[ "${USE_DOCLING}" == "true" ]] || { echo "[entrypoint] docling 功能未启用（USE_DOCLING!=true）"; return 0; }

  "${PY}" -c 'import pip' >/dev/null 2>&1 || "${PY}" -m ensurepip --upgrade || true
  DOCLING_PIN="${DOCLING_VERSION:-==2.58.0}"
  "${PY}" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('docling') else 1)" \
    || "${PY}" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://pypi.org/simple --no-cache-dir "docling${DOCLING_PIN}"
}

ensure_mineru() {
  [[ "${USE_MINERU}" == "true" ]] || { echo "[entrypoint] mineru 功能未启用（USE_MINERU!=true）"; return 0; }

  export HUGGINGFACE_HUB_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

  local default_prefix="${MINERU_HOME:-/multirag/uv_tools}"
  local venv_dir="${default_prefix}/.venv"
  local exe="${MINERU_EXECUTABLE:-${venv_dir}/bin/mineru}"

  if [[ -x "${exe}" ]]; then
    echo "[entrypoint] mineru 已检测到: ${exe}"
    export MINERU_EXECUTABLE="${exe}"
    return 0
  fi

  echo "[entrypoint] mineru 未安装，正使用 uv 初始化..."

  (
    set -e
    mkdir -p "${default_prefix}"
    cd "${default_prefix}"
    [[ -d "${venv_dir}" ]] || uv venv "${venv_dir}"

    # shellcheck source=/dev/null
    source "${venv_dir}/bin/activate"
    uv pip install -U "mineru[core]" -i https://mirrors.aliyun.com/pypi/simple --extra-index-url https://pypi.org/simple
    deactivate
  )

  export MINERU_EXECUTABLE="${exe}"
  if ! "${MINERU_EXECUTABLE}" --help >/dev/null 2>&1; then
    echo "[entrypoint] mineru 安装失败：${MINERU_EXECUTABLE} 无法运行" >&2
    return 1
  fi
  echo "[entrypoint] mineru 安装完成: ${MINERU_EXECUTABLE}"
}

_term() {
  echo "[entrypoint] 收到终止信号，清理子进程..."
  pkill -TERM -P $$ || true
  wait
  exit 0
}
trap _term SIGTERM SIGINT

# --------------------------- 启动流程 ---------------------------------------
ensure_docling
ensure_mineru

# 启动 Nginx（优先启动，作为统一入口）
if [[ "${ENABLE_WEBSERVER}" -eq 1 ]]; then
  start_nginx
fi

if [[ "${ENABLE_REDIS}" -eq 1 ]]; then
  start_redis
fi

if [[ "${ENABLE_ADMINSERVER}" -eq 1 ]]; then
  start_admin_server
fi

if [[ "${ENABLE_DATASYNC}" -eq 1 ]]; then
  echo "[entrypoint] 启动数据源同步服务..."
  while true; do
    "${PY}" -m core.svr.sync_data_source || true
    echo "[entrypoint] 数据源同步服务崩溃，1 秒后重启..."
    sleep 1
  done &
fi

if [[ "${ENABLE_TASKEXECUTOR}" -eq 1 ]]; then
  if (( CONSUMER_NO_END > CONSUMER_NO_BEG )); then
    echo "[entrypoint] 启动 TaskExecutors (ID 区间 [${CONSUMER_NO_BEG}, ${CONSUMER_NO_END}))，Host=${HOST_ID}"
    for (( i=CONSUMER_NO_BEG; i<CONSUMER_NO_END; i++ )); do
      task_exe "${i}" "${HOST_ID}"
    done
  else
    echo "[entrypoint] 启动 ${WORKERS} 个 TaskExecutor，Host=${HOST_ID}"
    for (( i=0; i<WORKERS; i++ )); do
      task_exe "${i}" "${HOST_ID}"
    done
  fi
fi

if [[ "${ENABLE_SERVER}" -eq 1 ]]; then
  start_server
else
  wait
fi
