#!/bin/sh
set -eu

SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CAPTURE_SCRIPT="$SELF_DIR/capture_memory_pressure.sh"

PRESSURE_STATE_DIR="${PRESSURE_STATE_DIR:-/home/arch/tmp/open-webui-pressure-watch}"
PRESSURE_POLL_SEC="${PRESSURE_POLL_SEC:-30}"
PRESSURE_CAPTURE_COOLDOWN_SEC="${PRESSURE_CAPTURE_COOLDOWN_SEC:-900}"
PRESSURE_MEM_AVAILABLE_MB_THRESHOLD="${PRESSURE_MEM_AVAILABLE_MB_THRESHOLD:-256}"
PRESSURE_SWAP_USED_MB_THRESHOLD="${PRESSURE_SWAP_USED_MB_THRESHOLD:-768}"
PRESSURE_SLAB_MB_THRESHOLD="${PRESSURE_SLAB_MB_THRESHOLD:-768}"
PRESSURE_SUNRECLAIM_MB_THRESHOLD="${PRESSURE_SUNRECLAIM_MB_THRESHOLD:-768}"

mkdir -p "$PRESSURE_STATE_DIR"
LAST_CAPTURE_FILE="$PRESSURE_STATE_DIR/last_capture.epoch"

read_meminfo_kb() {
  awk -v key="$1" '$1 == key ":" { print $2; exit }' /proc/meminfo
}

last_capture_epoch() {
  if [ -f "$LAST_CAPTURE_FILE" ]; then
    cat "$LAST_CAPTURE_FILE"
  else
    printf '0\n'
  fi
}

record_capture_epoch() {
  date +%s > "$LAST_CAPTURE_FILE"
}

printf '%s pressure_watch event=start poll_sec=%s mem_available_mb_threshold=%s swap_used_mb_threshold=%s slab_mb_threshold=%s sunreclaim_mb_threshold=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "$PRESSURE_POLL_SEC" \
  "$PRESSURE_MEM_AVAILABLE_MB_THRESHOLD" \
  "$PRESSURE_SWAP_USED_MB_THRESHOLD" \
  "$PRESSURE_SLAB_MB_THRESHOLD" \
  "$PRESSURE_SUNRECLAIM_MB_THRESHOLD"

while :; do
  mem_available_mb=$(( $(read_meminfo_kb MemAvailable) / 1024 ))
  swap_total_kb="$(read_meminfo_kb SwapTotal)"
  swap_free_kb="$(read_meminfo_kb SwapFree)"
  swap_used_mb=$(( (swap_total_kb - swap_free_kb) / 1024 ))
  slab_mb=$(( $(read_meminfo_kb Slab) / 1024 ))
  sunreclaim_mb=$(( $(read_meminfo_kb SUnreclaim) / 1024 ))

  trigger_parts=""
  if [ "$mem_available_mb" -le "$PRESSURE_MEM_AVAILABLE_MB_THRESHOLD" ]; then
    trigger_parts="${trigger_parts} mem_available_mb=$mem_available_mb"
  fi
  if [ "$swap_used_mb" -ge "$PRESSURE_SWAP_USED_MB_THRESHOLD" ]; then
    trigger_parts="${trigger_parts} swap_used_mb=$swap_used_mb"
  fi
  if [ "$slab_mb" -ge "$PRESSURE_SLAB_MB_THRESHOLD" ]; then
    trigger_parts="${trigger_parts} slab_mb=$slab_mb"
  fi
  if [ "$sunreclaim_mb" -ge "$PRESSURE_SUNRECLAIM_MB_THRESHOLD" ]; then
    trigger_parts="${trigger_parts} sunreclaim_mb=$sunreclaim_mb"
  fi

  if [ -n "$trigger_parts" ]; then
    now_epoch="$(date +%s)"
    last_epoch="$(last_capture_epoch)"
    elapsed_sec=$((now_epoch - last_epoch))
    if [ "$elapsed_sec" -ge "$PRESSURE_CAPTURE_COOLDOWN_SEC" ]; then
      reason="threshold_exceeded:${trigger_parts# }"
      printf '%s pressure_watch event=capture_triggered reason="%s"\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$reason"
      PRESSURE_REASON="$reason" "$CAPTURE_SCRIPT" >/dev/null || true
      record_capture_epoch
    else
      printf '%s pressure_watch event=cooldown_active remaining_sec=%s%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$((PRESSURE_CAPTURE_COOLDOWN_SEC - elapsed_sec))" \
        "$trigger_parts"
    fi
  fi

  sleep "$PRESSURE_POLL_SEC"
done
