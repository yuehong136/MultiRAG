#!/bin/bash

export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/

PY=python3
if [[ -z "$WS" || $WS -lt 1 ]]; then
  WS=1
fi

# 使用指定的配置文件启动 Redis 并在后台运行
redis-server /etc/redis/redis.conf
if [ $? -ne 0 ]; then
  echo "Failed to start Redis with /etc/redis/redis.conf"
  exit 1
fi
echo "Redis started successfully with /etc/redis/redis.conf"

function task_exe(){
    JEMALLOC_PATH=$(pkg-config --variable=libdir jemalloc)/libjemalloc.so
    while [ 1 -eq 1 ];do
      LD_PRELOAD=$JEMALLOC_PATH $PY -m core.svr.task_executor $1;
    done
}

for ((i=0;i<WS;i++))
do
  task_exe  $i &
done

while [ 1 -eq 1 ];do
    $PY -m api.multirag_server
done

wait;