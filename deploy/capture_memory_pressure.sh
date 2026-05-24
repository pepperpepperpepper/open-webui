#!/bin/sh
set -eu

OUTPUT_DIR="${PRESSURE_OUTPUT_DIR:-/home/arch/logs}"
OUTPUT_PREFIX="${PRESSURE_OUTPUT_PREFIX:-memory-pressure}"
TAIL_LINES="${PRESSURE_LOG_TAIL_LINES:-200}"
OPEN_WEBUI_LOG_PATH="${OPEN_WEBUI_LOG_PATH:-/home/arch/logs/open-webui/current}"
LIVEKIT_AGENT_LOG_PATH="${LIVEKIT_AGENT_LOG_PATH:-/home/arch/logs/open-webui-livekit-agent/current}"
LIVEKIT_PORTAL_LOG_PATH="${LIVEKIT_PORTAL_LOG_PATH:-/home/arch/logs/open-webui-livekit-portal/current}"

REASON="${PRESSURE_REASON:-manual}"

usage() {
  cat <<'EOF'
Usage: capture_memory_pressure.sh [--reason TEXT] [--output-dir DIR] [--tail-lines N]
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --reason)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      REASON="$2"
      shift 2
      ;;
    --output-dir)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --tail-lines)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      TAIL_LINES="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

timestamp_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
timestamp_slug="$(date -u +%Y%m%dT%H%M%SZ)"
hostname_value="$(hostname)"
mkdir -p "$OUTPUT_DIR"
OUTPUT_PATH="$OUTPUT_DIR/$OUTPUT_PREFIX-$timestamp_slug.log"

read_meminfo_kb() {
  awk -v key="$1" '$1 == key ":" { print $2; exit }' /proc/meminfo
}

mem_total_kb="$(read_meminfo_kb MemTotal)"
mem_available_kb="$(read_meminfo_kb MemAvailable)"
swap_total_kb="$(read_meminfo_kb SwapTotal)"
swap_free_kb="$(read_meminfo_kb SwapFree)"
slab_kb="$(read_meminfo_kb Slab)"
sunreclaim_kb="$(read_meminfo_kb SUnreclaim)"
swap_used_kb=$((swap_total_kb - swap_free_kb))

append_line() {
  printf '%s\n' "$1" >> "$OUTPUT_PATH"
}

append_section() {
  append_line ""
  append_line "===== $1 ====="
}

append_cmd() {
  title="$1"
  shift
  append_section "$title"
  append_line "+ $*"
  if "$@" >> "$OUTPUT_PATH" 2>&1; then
    :
  else
    status="$?"
    append_line "[exit $status]"
  fi
}

append_cmd_sh() {
  title="$1"
  shift
  command_text="$1"
  append_section "$title"
  append_line "+ $command_text"
  if sh -lc "$command_text" >> "$OUTPUT_PATH" 2>&1; then
    :
  else
    status="$?"
    append_line "[exit $status]"
  fi
}

append_line "timestamp_utc=$timestamp_iso"
append_line "hostname=$hostname_value"
append_line "reason=$REASON"
append_line "mem_total_mb=$((mem_total_kb / 1024))"
append_line "mem_available_mb=$((mem_available_kb / 1024))"
append_line "swap_used_mb=$((swap_used_kb / 1024))"
append_line "swap_total_mb=$((swap_total_kb / 1024))"
append_line "slab_mb=$((slab_kb / 1024))"
append_line "sunreclaim_mb=$((sunreclaim_kb / 1024))"

append_cmd "Uptime" uptime
append_cmd "Load Average" cat /proc/loadavg
append_cmd "Memory Pressure Stall Information" cat /proc/pressure/memory
append_cmd "CPU Pressure Stall Information" cat /proc/pressure/cpu
append_cmd "IO Pressure Stall Information" cat /proc/pressure/io
append_cmd "free -h" free -h
append_cmd_sh "Selected /proc/meminfo" "grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|Slab|SReclaimable|SUnreclaim|KernelStack|PageTables):' /proc/meminfo"
append_cmd "vmstat sample" vmstat 1 5
append_cmd_sh "Top RSS processes" "ps -eo pid,ppid,user,stat,etime,rss,vsz,comm,args --sort=-rss | head -n 40"
append_cmd_sh "Top VSZ processes" "ps -eo pid,ppid,user,stat,etime,rss,vsz,comm,args --sort=-vsz | head -n 40"
append_cmd_sh "Tasks in D state" "ps -eo state,pid,ppid,user,wchan:32,comm,args | awk 'NR==1 || \$1 ~ /D/'"
append_cmd "Socket summary" ss -s
append_cmd_sh "Half-open public sockets" "ss -tan state syn-recv '( sport = :443 or sport = :80 or sport = :22 )'"
append_cmd_sh "TCP retransmit counters" "netstat -s 2>/dev/null | grep -E 'listen|SYN|retransmit|embryonic|pruned|abort' || true"
append_cmd_sh "Slab summary" "slabtop -o -s c | head -n 40"
append_cmd_sh "Interesting slab caches" "grep -E 'task_struct|signal_cache|pid |mem_cgroup|kernfs_node_cache|kmalloc|sock_inode_cache|tcp_bind_bucket|request_sock|tw_sock' /proc/slabinfo | head -n 80"
append_cmd_sh "Kernel messages (tail)" "dmesg -T 2>/dev/null | tail -n 120"

if command -v curl >/dev/null 2>&1; then
  append_cmd_sh "Open WebUI local probe" "curl -sS -o /dev/null -w 'open_webui_local %{http_code} %{time_total}\\n' --max-time 5 http://127.0.0.1:8080/"
  append_cmd_sh "LiveKit portal local probe" "curl -sS -o /dev/null -w 'livekit_portal_local %{http_code} %{time_total}\\n' --max-time 5 http://127.0.0.1:8092/"
fi

if [ -f "$OPEN_WEBUI_LOG_PATH" ]; then
  append_cmd_sh "Open WebUI log tail" "tail -n $TAIL_LINES '$OPEN_WEBUI_LOG_PATH'"
fi

if [ -f "$LIVEKIT_AGENT_LOG_PATH" ]; then
  append_cmd_sh "LiveKit agent session_metric tail" "tail -n $TAIL_LINES '$LIVEKIT_AGENT_LOG_PATH' | grep -E 'session_metric|worker is at full capacity|not dispatching agent job|process is unresponsive|turn_detector|languages.json' || true"
fi

if [ -f "$LIVEKIT_PORTAL_LOG_PATH" ]; then
  append_cmd_sh "LiveKit portal portal_metric tail" "tail -n $TAIL_LINES '$LIVEKIT_PORTAL_LOG_PATH' | grep -E 'portal_metric|apply_conflict|leave_deleted|token_issued|room_not_found' || true"
fi

append_line ""
append_line "saved_to=$OUTPUT_PATH"
printf '%s\n' "$OUTPUT_PATH"
