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
    --workers=*)            WORKERS="${arg#*=}" ; shift ;;
    --consumer-no-beg=*)    CONSUMER_NO_BEG="${arg#*=}" ; shift ;;
    --consumer-no-end=*)    CONSUMER_NO_END="${arg#*=}" ; shift ;;
    --host-id=*)            HOST_ID="${arg#*=}" ; shift ;;
    -h|--help)              usage ;;
    *)                      echo "未知参数: $arg" ; usage ;;
  esac
done

# --------------------------- 函数定义 ---------------------------------------
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

_term() {
  echo "[entrypoint] 收到终止信号，清理子进程..."
  pkill -TERM -P $$ || true
  wait
  exit 0
}
trap _term SIGTERM SIGINT

# --------------------------- 启动流程 ---------------------------------------
if [[ "${ENABLE_REDIS}" -eq 1 ]]; then
  start_redis
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
