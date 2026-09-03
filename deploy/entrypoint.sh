#!/bin/sh
set -eu
umask 077

CONFIG_PATH="${NESTRA_CONFIG:-/app/config/config.yaml}"
DATA_DIR="${NESTRA_DATA_DIR:-/app/data}"

json_event() {
    # 事件名均为本文件内静态常量，不接收用户输入。
    printf '{"event":"%s","level":"info","logger":"nestra.entrypoint"}\n' "$1"
}

if [ ! -d "$DATA_DIR" ] || [ ! -w "$DATA_DIR" ]; then
    printf '%s\n' \
        '{"event":"data_directory_not_writable","level":"error","logger":"nestra.entrypoint"}' >&2
    printf '%s\n' \
        '{"event":"chown_data_to_non_root_uid_gid","level":"error","logger":"nestra.entrypoint"}' >&2
    printf '%s\n' \
        '{"event":"uid_gid_must_not_be_zero","level":"error","logger":"nestra.entrypoint"}' >&2
    exit 1
fi

# 默认服务启动前 fail-fast。CLI 的人类可读成功摘要丢弃；若失败，错误日志仍以 JSON
# 写到 stderr 并由 `set -e` 阻止启动。FastAPI lifespan 会再次幂等校验/迁移。
if [ "$#" -ge 2 ] && [ "$1" = "nestra" ] && [ "$2" = "serve" ]; then
    json_event "configuration_validation_started"
    nestra config check --config "$CONFIG_PATH" --log-format json >/dev/null

    json_event "database_migration_started"
    nestra db migrate --config "$CONFIG_PATH" --log-format json >/dev/null

    json_event "application_exec"
    exec "$@" --log-format json
fi

# 允许 `docker run IMAGE nestra version` 等维护命令，不擅自迁移。
exec "$@"
