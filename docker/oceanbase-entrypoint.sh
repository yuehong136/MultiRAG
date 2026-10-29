#!/bin/bash
# PID 1 wrapper for the pinned OceanBase CE image. Never enable shell tracing here.
set -euo pipefail

log() { printf '[multirag-ob-entrypoint] %s\n' "$*" >&2; }
fail() { log "error: $*"; exit 1; }

OB_TENANT_NAME=${OB_TENANT_NAME:-multirag}
OB_SYS_PASSWORD=${OB_SYS_PASSWORD-${OCEANBASE_PASSWORD:-dxl_multi_rag}}
OB_TENANT_PASSWORD=${OB_TENANT_PASSWORD-${OCEANBASE_PASSWORD:-dxl_multi_rag}}
OCEANBASE_DOC_DBNAME=${OCEANBASE_DOC_DBNAME:-multirag_doc}
OB_READY_FILE=${OB_READY_FILE:-/tmp/multirag-oceanbase-ready}
OB_WAIT_ATTEMPTS=${OB_WAIT_ATTEMPTS:-120}
OB_COMMAND_TIMEOUT=${OB_COMMAND_TIMEOUT:-30}
OB_STOP_TIMEOUT=${OB_STOP_TIMEOUT:-10}

# Restrict identifiers before they reach either SQL or obd's option parser.
for name in "$OB_TENANT_NAME" "$OCEANBASE_DOC_DBNAME"; do
  [[ $name =~ ^[a-zA-Z][a-zA-Z0-9_]{0,63}$ ]] || fail 'invalid tenant/database identifier'
done
case $(printf %s "$OB_TENANT_NAME" | tr '[:upper:]' '[:lower:]') in sys|mysql|oceanbase|information_schema) fail 'reserved tenant name';; esac
case $(printf %s "$OCEANBASE_DOC_DBNAME" | tr '[:upper:]' '[:lower:]') in sys|mysql|oceanbase|information_schema|performance_schema) fail 'reserved database name';; esac
for number in "$OB_WAIT_ATTEMPTS" "$OB_COMMAND_TIMEOUT" "$OB_STOP_TIMEOUT"; do
  [[ $number =~ ^[1-9][0-9]{0,3}$ ]] || fail 'timeouts/attempts must be integers from 1 to 9999'
done
for password in "$OB_SYS_PASSWORD" "$OB_TENANT_PASSWORD"; do
  [[ $password != *$'\n'* && $password != *$'\r'* ]] || fail 'passwords cannot contain line breaks'
done

start_pid= active_pid= owns_ready=0
stop_pid() {
  local pid=$1 remaining=$OB_STOP_TIMEOUT
  kill -TERM "$pid" 2>/dev/null || return 0
  while kill -0 "$pid" 2>/dev/null && (( remaining > 0 )); do
    sleep 1
    remaining=$((remaining - 1))
  done
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}
cleanup() {
  if [[ $owns_ready == 1 ]]; then rm -f -- "$OB_READY_FILE"; fi
  if [[ -n $active_pid ]]; then stop_pid "$active_pid"; fi
  if [[ -n $start_pid ]]; then stop_pid "$start_pid"; fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

# Bound even stalled client/obd commands, not just the retry loop.
bounded() {
  local result=0
  "$@" <&0 &
  active_pid=$!
  local deadline=$((SECONDS + OB_COMMAND_TIMEOUT))
  while kill -0 "$active_pid" 2>/dev/null; do
    if (( SECONDS >= deadline )); then
      stop_pid "$active_pid"
      active_pid=
      return 124
    fi
    sleep 0.1
  done
  wait "$active_pid" || result=$?
  active_pid=
  return "$result"
}
client() {
  local user=$1 password=$2 sql=$3
  shift 3
  # MYSQL_PWD avoids exposing credentials in argv; client output is never logged.
  bounded env MYSQL_PWD="$password" obclient \
    --connect-timeout=5 -h127.0.0.1 -P2881 "-u$user" -A -N -B "$@" <<< "$sql" 2>/dev/null
}
sys() { client root@sys "$OB_SYS_PASSWORD" "$1" -Doceanbase; }
tenant() { client "root@$OB_TENANT_NAME" "$OB_TENANT_PASSWORD" "$1" "${@:2}"; }

if [[ ${1:-} == --healthcheck ]]; then
  # Probe only; never create databases or repair passwords from a healthcheck.
  [[ -f $OB_READY_FILE ]] && tenant 'SELECT 1' "-D$OCEANBASE_DOC_DBNAME" >/dev/null
  exit $?
fi
[[ $# == 0 ]] || fail 'unsupported argument'
owns_ready=1
rm -f -- "$OB_READY_FILE"

alive() { kill -0 "$start_pid" 2>/dev/null || fail 'start child exited before reconciliation completed'; }
wait_query() {
  local kind=$1 attempt
  for (( attempt=0; attempt<OB_WAIT_ATTEMPTS; attempt++ )); do
    alive
    if "$kind" 'SELECT 1' >/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

# These paths are provided by oceanbase/oceanbase-ce; overrides support isolated tests.
"${OB_SSHD_PATH:-/usr/sbin/sshd}" >/dev/null 2>&1 || fail 'sshd failed to start'
"${OB_START_PATH:-/root/boot/start.sh}" &
start_pid=$!
wait_query sys || fail 'sys tenant readiness timed out (check sys credentials and server logs)'

if ! tenant 'SELECT 1' >/dev/null; then
  exists=$(sys "SELECT COUNT(*) FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name='$OB_TENANT_NAME'") || fail 'cannot query tenant catalog'
  case $exists in
    0)
      log 'creating missing tenant'
      # obd has no password-file option; it receives the password in argv. Suppress
      # its output and do not echo the command; restrict container process access.
      bounded obd cluster tenant create obcluster -n "$OB_TENANT_NAME" \
        -o "${OB_SCENARIO:-htap}" --password "$OB_TENANT_PASSWORD" >/dev/null 2>&1 || fail 'tenant creation failed'
      ;;
    1)
      accepted=0
      for (( attempt=0; attempt<OB_WAIT_ATTEMPTS; attempt++ )); do
        alive
        if tenant 'SELECT 1' >/dev/null; then accepted=1; break; fi
        if client "root@$OB_TENANT_NAME" '' 'SELECT 1' >/dev/null; then
          log 'applying configured password to blank-password tenant'
          escaped=${OB_TENANT_PASSWORD//\\/\\\\}
          escaped=${escaped//\'/\'\'}
          client "root@$OB_TENANT_NAME" '' "SET SESSION sql_mode=''; ALTER USER root IDENTIFIED BY '$escaped'" >/dev/null || fail 'blank tenant password update failed'
          accepted=1
          break
        fi
        sleep 1
      done
      [[ $accepted == 1 ]] || fail 'existing tenant rejects configured and blank passwords; refusing password reset'
      ;;
    *) fail 'unexpected tenant catalog response';;
  esac
  wait_query tenant || fail 'tenant readiness timed out after reconciliation'
fi
alive
tenant "CREATE DATABASE IF NOT EXISTS \`$OCEANBASE_DOC_DBNAME\`" >/dev/null || fail 'database creation failed'
tenant 'SELECT 1' "-D$OCEANBASE_DOC_DBNAME" >/dev/null || fail 'database readiness failed'
alive
: > "$OB_READY_FILE"
log 'tenant and database reconciliation complete'
result=0
wait "$start_pid" || result=$?
start_pid=
exit "$result"
