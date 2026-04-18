#!/bin/bash
#
# 灵犀持久化启动脚本
# 支持后台运行，关闭终端不影响
#

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 项目根目录
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [ -z "${DDDD2_PATH:-}" ] && [ -f "$PROJECT_DIR/dddd2" ]; then
    export DDDD2_PATH="$PROJECT_DIR/dddd2"
fi

MAIN_SCRIPT="$PROJECT_DIR/main.py"
LOG_FILE="$PROJECT_DIR/lingxi.log"
PID_FILE="$PROJECT_DIR/lingxi.pid"

# 默认参数
MODE="default"
WEB_ENABLED=false
WEB_PORT=8899
ACTION="start"
GRACEFUL_WAIT_SECONDS=20
TERM_WAIT_SECONDS=5

resolve_python_cmd() {
    local candidate=""

    if [ -n "${LING_XI_PYTHON:-}" ] && [ -x "${LING_XI_PYTHON:-}" ]; then
        echo "$LING_XI_PYTHON"
        return 0
    fi

    for candidate in "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/venv/bin/python"; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done

    return 1
}

validate_port() {
    local port=$1
    if [[ ! "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        echo -e "${RED}❌ 非法端口: $port${NC}"
        exit 1
    fi
}

set_action() {
    local next_action=$1
    if [ "$ACTION" != "start" ] && [ "$ACTION" != "$next_action" ]; then
        echo -e "${RED}❌ --stop、--status、--logs 只能选择一个${NC}"
        exit 1
    fi
    ACTION="$next_action"
}

set_mode() {
    local next_mode=$1
    if [ "$MODE" != "default" ] && [ "$MODE" != "$next_mode" ]; then
        echo -e "${RED}❌ --lj 与 --all 只能选择一个${NC}"
        exit 1
    fi
    MODE="$next_mode"
}

pid_state() {
    local pid=$1
    ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]'
}

pid_exists() {
    local pid=$1
    local state=""

    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    state=$(pid_state "$pid")
    [ -n "$state" ] && [[ "$state" != *Z* ]]
}

read_pid_file() {
    local pid_file=$1
    [ -f "$pid_file" ] || return 0
    tr -cd '0-9' < "$pid_file"
}

cleanup_pid_file() {
    local pid_file=$1
    local pid=""

    [ -f "$pid_file" ] || return 0
    pid=$(read_pid_file "$pid_file")
    if [ -z "$pid" ] || ! pid_exists "$pid"; then
        rm -f "$pid_file"
    fi
}

wait_for_pid_list() {
    local timeout=$1
    shift
    local pids=("$@")
    local waited=0
    local pid=""

    while [ "$waited" -lt "$timeout" ]; do
        local alive=0
        for pid in "${pids[@]}"; do
            if pid_exists "$pid"; then
                alive=1
                break
            fi
        done
        if [ "$alive" -eq 0 ]; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

collect_live_pids() {
    local pid=""
    for pid in "$@"; do
        if pid_exists "$pid"; then
            echo "$pid"
        fi
    done
}

append_unique_pid() {
    local pid=$1
    local existing=""

    [ -n "$pid" ] || return 0
    for existing in "${DAEMON_PIDS[@]:-}"; do
        if [ "$existing" = "$pid" ]; then
            return 0
        fi
    done
    DAEMON_PIDS+=("$pid")
}

join_pids() {
    local IFS=,
    echo "$*"
}

command_for_pid() {
    local pid=$1
    ps -p "$pid" -o args= 2>/dev/null
}

collect_pids_from_scan() {
    local needle=$1
    ps -eo pid=,args= | awk -v needle="$needle" '
        {
            pid = $1
            $1 = ""
            sub(/^ +/, "", $0)
            if (index($0, needle) > 0 && index($0, "awk -v needle=") == 0 && index($0, "ps -eo pid=,args=") == 0) {
                print pid
            }
        }
    '
}

collect_daemon_pids() {
    DAEMON_PIDS=()
    local pid=""
    local args=""

    cleanup_pid_file "$PID_FILE"
    pid=$(read_pid_file "$PID_FILE")
    [ -n "$pid" ] && append_unique_pid "$pid"

    while read -r pid; do
        args=$(command_for_pid "$pid")
        [ -n "$args" ] || continue
        if [[ "$args" == *"$MAIN_SCRIPT"* ]] \
            && [[ "$args" != *"--web-only"* ]] \
            && [[ "$args" != *"--main-only"* ]] \
            && [[ "$args" != *"--forum-only"* ]]; then
            append_unique_pid "$pid"
        fi
    done < <(collect_pids_from_scan "$MAIN_SCRIPT")

    if [ ${#DAEMON_PIDS[@]} -gt 0 ]; then
        printf '%s\n' "${DAEMON_PIDS[@]}"
    fi
}

sync_pid_file_from_list() {
    local pid_file=$1
    shift

    if [ "$#" -eq 1 ]; then
        printf '%s\n' "$1" > "$pid_file"
    else
        rm -f "$pid_file"
    fi
}

show_help() {
    echo "灵犀持久化启动脚本"
    echo ""
    echo "用法:"
    echo "  $0 [选项]"
    echo ""
    echo "选项:"
    echo "  --lj              只运行零界论坛赛道"
    echo "  --all             主战场 + 论坛双开"
    echo "  --web             启动 Web Dashboard"
    echo "  --port PORT       指定 Web 端口 (默认: 8899)"
    echo "  --stop            停止运行中的灵犀"
    echo "  --status          查看运行状态"
    echo "  --logs            实时查看日志"
    echo "  --help            显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  $0                # 后台运行主战场"
    echo "  $0 --all --web    # 双开并启动Dashboard"
    echo "  $0 --stop         # 停止运行"
    echo "  $0 --logs         # 查看实时日志"
}

show_status() {
    local daemon_pids=()
    mapfile -t daemon_pids < <(collect_daemon_pids)

    if [ ${#daemon_pids[@]} -eq 0 ]; then
        echo -e "${YELLOW}灵犀未运行${NC}"
        return 0
    fi

    sync_pid_file_from_list "$PID_FILE" "${daemon_pids[@]}"
    echo -e "${GREEN}✅ 灵犀正在运行 (PID: $(join_pids "${daemon_pids[@]}"))${NC}"
    if [ ${#daemon_pids[@]} -eq 1 ]; then
        echo "启动命令: $(command_for_pid "${daemon_pids[0]}")"
    fi
    echo "日志文件: $LOG_FILE"

    if [ -f "$LOG_FILE" ]; then
        echo ""
        echo "最近10行日志:"
        tail -n 10 "$LOG_FILE"
    fi
}

stop_daemon() {
    local daemon_pids=()
    local remaining=()

    mapfile -t daemon_pids < <(collect_daemon_pids)
    if [ ${#daemon_pids[@]} -eq 0 ]; then
        echo -e "${RED}❌ 灵犀未运行${NC}"
        return 0
    fi

    sync_pid_file_from_list "$PID_FILE" "${daemon_pids[@]}"
    echo -e "${YELLOW}正在停止灵犀 (PID: $(join_pids "${daemon_pids[@]}"))...${NC}"
    kill -INT "${daemon_pids[@]}" 2>/dev/null || true

    if ! wait_for_pid_list "$GRACEFUL_WAIT_SECONDS" "${daemon_pids[@]}"; then
        mapfile -t remaining < <(collect_live_pids "${daemon_pids[@]}")
        if [ ${#remaining[@]} -gt 0 ]; then
            echo -e "${YELLOW}⏳ 灵犀尚未退出，升级为 SIGTERM...${NC}"
            kill -TERM "${remaining[@]}" 2>/dev/null || true
        fi
    fi

    if ! wait_for_pid_list "$TERM_WAIT_SECONDS" "${daemon_pids[@]}"; then
        mapfile -t remaining < <(collect_live_pids "${daemon_pids[@]}")
        if [ ${#remaining[@]} -gt 0 ]; then
            echo -e "${YELLOW}⚠️  灵犀仍未退出，执行强制终止...${NC}"
            kill -KILL "${remaining[@]}" 2>/dev/null || true
            wait_for_pid_list 2 "${remaining[@]}" || true
        fi
    fi

    rm -f "$PID_FILE"
    echo -e "${GREEN}✅ 灵犀已停止${NC}"
}

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --lj)
            set_mode "lj"
            shift
            ;;
        --all)
            set_mode "all"
            shift
            ;;
        --web)
            WEB_ENABLED=true
            shift
            ;;
        --port)
            if [ $# -lt 2 ]; then
                echo -e "${RED}❌ --port 需要指定端口${NC}"
                exit 1
            fi
            validate_port "$2"
            WEB_PORT="$2"
            shift 2
            ;;
        --stop)
            set_action "stop"
            shift
            ;;
        --status)
            set_action "status"
            shift
            ;;
        --logs)
            set_action "logs"
            shift
            ;;
        --help)
            show_help
            exit 0
            ;;
        *)
            echo -e "${RED}未知参数: $1${NC}"
            echo "使用 --help 查看帮助"
            exit 1
            ;;
    esac
done

case "$ACTION" in
    stop)
        stop_daemon
        exit 0
        ;;
    status)
        show_status
        exit 0
        ;;
    logs)
        if [ -f "$LOG_FILE" ]; then
            tail -F "$LOG_FILE"
            exit 0
        fi
        echo -e "${RED}❌ 日志文件不存在${NC}"
        exit 1
        ;;
esac

if ! PYTHON_CMD=$(resolve_python_cmd); then
    echo -e "${RED}❌ 未找到可用的 Python 解释器${NC}"
    echo "请先运行: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

existing_daemon_pids=()
mapfile -t existing_daemon_pids < <(collect_daemon_pids)
if [ ${#existing_daemon_pids[@]} -gt 0 ]; then
    sync_pid_file_from_list "$PID_FILE" "${existing_daemon_pids[@]}"
    echo -e "${RED}❌ 灵犀已在运行 (PID: $(join_pids "${existing_daemon_pids[@]}"))${NC}"
    echo "使用 '$0 --stop' 停止现有进程"
    exit 1
fi

CMD_ARGS=()
case $MODE in
    lj)
        CMD_ARGS+=("--lj")
        ;;
    all)
        CMD_ARGS+=("--all")
        ;;
esac

if [ "$WEB_ENABLED" = true ]; then
    CMD_ARGS+=("--web" "--port" "$WEB_PORT")
fi

echo -e "${GREEN}🚀 启动灵犀 (后台模式)${NC}"
echo "模式: $MODE"
echo "Python: $PYTHON_CMD"
echo "Web Dashboard: $WEB_ENABLED"
[ "$WEB_ENABLED" = true ] && echo "Web 端口: $WEB_PORT"
echo "日志文件: $LOG_FILE"
echo ""

nohup env LINGXI_LOG_FILE="$LOG_FILE" "$PYTHON_CMD" "$MAIN_SCRIPT" "${CMD_ARGS[@]}" > "$LOG_FILE" 2>&1 &
PID=$!
printf '%s\n' "$PID" > "$PID_FILE"

sleep 2

if pid_exists "$PID"; then
    echo -e "${GREEN}✅ 灵犀已启动 (PID: $PID)${NC}"
    echo ""
    echo "管理命令:"
    echo "  查看日志: $0 --logs"
    echo "  查看状态: $0 --status"
    echo "  停止运行: $0 --stop"
    if [ -f "$LOG_FILE" ]; then
        echo ""
        echo "最近5行日志:"
        tail -n 5 "$LOG_FILE"
    fi
else
    echo -e "${RED}❌ 启动失败，请查看日志: $LOG_FILE${NC}"
    rm -f "$PID_FILE"
    exit 1
fi
