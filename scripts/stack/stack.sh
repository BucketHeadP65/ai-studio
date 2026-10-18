#!/usr/bin/env bash
# Manages the four engram LaunchAgents: install/uninstall, start/stop/restart, status, logs.
# Usage:
#   stack.sh install
#   stack.sh uninstall
#   stack.sh start|stop|restart [service ...|all]
#   stack.sh status [service ...|all]
#   stack.sh logs [service ...|all]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"
LOG_DIR="$HOME/Library/Logs/engram"
AGENTS_DIR="$HOME/Library/LaunchAgents"

ALL_SERVICES=(db harness studio-api studio-client)

label_for() { echo "com.engram.$1"; }
plist_for() { echo "$AGENTS_DIR/$(label_for "$1").plist"; }
script_for() { echo "$SCRIPT_DIR/$1.sh"; }
log_for() { echo "$LOG_DIR/$1.log"; }
port_for() {
    case "$1" in
    db) echo 5434 ;;
    harness) echo 8770 ;;
    studio-api) echo 8100 ;;
    studio-client) echo 3031 ;;
    esac
}
# A route each HTTP service answers 200 on once it is up (none serves "/").
health_path_for() {
    case "$1" in
    harness) echo /api/sessions ;;
    studio-api) echo /api/knowledge-bases ;;
    studio-client) echo /chat ;;
    esac
}

is_known_service() {
    for s in "${ALL_SERVICES[@]}"; do
        [[ "$s" == "$1" ]] && return 0
    done
    return 1
}

# Prints the service list a subcommand should act on: every known service given no
# arguments or "all", else exactly the services named.
resolve_services() {
    if [[ $# -eq 0 || ("$#" -eq 1 && "$1" == "all") ]]; then
        printf '%s\n' "${ALL_SERVICES[@]}"
        return
    fi
    for s in "$@"; do
        if ! is_known_service "$s"; then
            echo "unknown service: $s (expected one of: ${ALL_SERVICES[*]})" >&2
            exit 1
        fi
        echo "$s"
    done
}

is_loaded() {
    launchctl print "$DOMAIN/$(label_for "$1")" >/dev/null 2>&1
}

render_plist() {
    local service="$1" label plist script log
    label="$(label_for "$service")"
    plist="$(plist_for "$service")"
    script="$(script_for "$service")"
    log="$(log_for "$service")"
    cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$script</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$log</string>
    <key>StandardErrorPath</key>
    <string>$log</string>
</dict>
</plist>
PLIST
}

# Whether the process listening on `service`'s port belongs to this checkout, so it is
# safe to terminate and hand the port to launchd. Checked by the listener's working
# directory (must be this checkout's app directory) and its command line (must name the
# app). A listener that fails either check is somebody else's and is left alone.
# lsof exits non-zero on a free port; that is the "no listener" answer, not a failure.
listener_pid() {
    lsof -nP -iTCP:"$(port_for "$1")" -sTCP:LISTEN -t 2>/dev/null | head -n1 || true
}

listener_is_ours() {
    local service="$1" pid="$2" want_dir cwd cmd want_pattern
    case "$service" in
    harness) want_dir="$REPO_ROOT/harness" want_pattern="harness" ;;
    studio-api) want_dir="$REPO_ROOT/studio" want_pattern="studio" ;;
    studio-client) want_dir="$REPO_ROOT/studio-client" want_pattern="next|node" ;;
    *) return 1 ;;
    esac
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
    cmd="$(ps -o command= -p "$pid" 2>/dev/null)"
    [[ "$cwd" == "$want_dir"* ]] && [[ "$cmd" =~ $want_pattern ]]
}

# Every live process that is `service`'s own: its command names the service's directory in
# this checkout, or matches the service's command pattern with its cwd under that directory.
# This is what a restart sweeps, because launchd tracks only the wrapper: `kickstart -k`
# SIGKILLs the tracked `uv run`, orphaning its python child (and serve-http's worker
# grandchild) to pid 1 with the port still bound and old code still serving.
service_pids() {
    local service="$1" dir pattern pid cmd cwd
    case "$service" in
    harness) dir="$REPO_ROOT/harness" pattern="harness serve|monty subprocess" ;;
    studio-api) dir="$REPO_ROOT/studio" pattern="studio serve-http|studio[._]corpus[._]cli worker|studio worker" ;;
    studio-client) dir="$REPO_ROOT/studio-client" pattern="next dev|next-server|pool_entry" ;;
    *) return 0 ;;
    esac
    # The pattern names the service's own commands (never a pytest, an editor's LSP, or an MCP
    # server living in the same directory); the directory, in the command line or as the cwd,
    # proves the process is this checkout's and not another clone's.
    ps -axo pid=,command= | while read -r pid cmd; do
        [[ "$pid" == "$$" ]] && continue
        [[ "$cmd" =~ $pattern ]] || continue
        if [[ "$cmd" == *"$dir"* ]]; then
            echo "$pid"
        else
            cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
            [[ "$cwd" == "$dir"* ]] && echo "$pid"
        fi
    done
}

# Sweeps every process `service_pids` names, saying which. Run only while the launchd job
# is booted out, so the sweep can never race a fresh incarnation it should leave alone.
# Two passes: a supervisor being TERMed can respawn its worker child mid-shutdown and then
# die, orphaning it; the second pass runs once the first's targets are confirmed dead, so
# nothing is left to respawn behind it.
sweep_service() {
    local service="$1" pid pass
    for pass in 1 2; do
        for pid in $(service_pids "$service"); do
            echo "  $service: sweeping stray pid $pid ($(ps -o command= -p "$pid" 2>/dev/null || echo gone))"
            terminate_pid "$pid"
        done
    done
}

# Stops a process this checkout owns so launchd can bind its port. TERM, wait, KILL.
terminate_pid() {
    local pid="$1" waited=0
    kill -TERM "$pid" 2>/dev/null || return 0
    while kill -0 "$pid" 2>/dev/null && [[ "$waited" -lt 10 ]]; do
        sleep 1
        waited=$((waited + 1))
    done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
}

# Frees each app port (db excluded -- its listener is Docker's own proxy, and
# `docker compose up --no-recreate` adopts an already-running container instead of a
# port takeover). Prints what it found; returns the services whose port is held by
# somebody else, which install then leaves stopped.
take_over_ports() {
    local service pid
    SKIP_SERVICES=()
    for service in harness studio-api studio-client; do
        pid="$(listener_pid "$service")"
        if [[ -z "$pid" ]]; then
            echo "  $service: port $(port_for "$service") is free"
            continue
        fi
        if listener_is_ours "$service" "$pid"; then
            echo "  $service: port $(port_for "$service") held by our own pid $pid ($(ps -o command= -p "$pid")) -- stopping it"
            terminate_pid "$pid"
        else
            echo "  $service: port $(port_for "$service") held by pid $pid ($(ps -o command= -p "$pid" 2>/dev/null)), not ours -- leaving it alone, skipping $service"
            SKIP_SERVICES+=("$service")
        fi
    done
}

cmd_install() {
    mkdir -p "$LOG_DIR" "$AGENTS_DIR"
    chmod +x "$SCRIPT_DIR"/*.sh
    echo "checking for existing listeners on stable ports..."
    take_over_ports
    if docker compose -f "$REPO_ROOT/engram/docker-compose.yml" ps --status running --services 2>/dev/null | grep -qx postgres; then
        echo "  db: postgres container already running -- docker compose will adopt it"
    fi
    for service in "${ALL_SERVICES[@]}"; do
        render_plist "$service"
        if is_loaded "$service"; then
            launchctl bootout "$DOMAIN/$(label_for "$service")" 2>/dev/null || true
        fi
        skip=false
        for s in "${SKIP_SERVICES[@]:-}"; do
            [[ "$s" == "$service" ]] && skip=true
        done
        if [[ "$skip" == true ]]; then
            echo "installed $(plist_for "$service") (not started -- port held by another process)"
            continue
        fi
        launchctl bootstrap "$DOMAIN" "$(plist_for "$service")"
        launchctl kickstart -k "$DOMAIN/$(label_for "$service")"
        echo "installed and started $service"
    done
}

cmd_uninstall() {
    for service in "${ALL_SERVICES[@]}"; do
        if is_loaded "$service"; then
            launchctl bootout "$DOMAIN/$(label_for "$service")"
        fi
        rm -f "$(plist_for "$service")"
        echo "removed $service"
    done
}

cmd_start() {
    for service in $(resolve_services "$@"); do
        if ! is_loaded "$service"; then
            launchctl bootstrap "$DOMAIN" "$(plist_for "$service")"
        fi
        launchctl kickstart -k "$DOMAIN/$(label_for "$service")"
        echo "started $service"
    done
}

cmd_stop() {
    for service in $(resolve_services "$@"); do
        if is_loaded "$service"; then
            launchctl bootout "$DOMAIN/$(label_for "$service")"
            echo "stopped $service"
        else
            echo "$service is not loaded"
        fi
        sweep_service "$service"
    done
}

cmd_restart() {
    for service in $(resolve_services "$@"); do
        # Bootout first so nothing launchd tracks survives, then sweep the strays it lost
        # (orphaned python children, worker grandchildren), then start clean. `kickstart -k`
        # alone kills only the tracked wrapper and leaves the real server serving old code.
        if is_loaded "$service"; then
            launchctl bootout "$DOMAIN/$(label_for "$service")" 2>/dev/null || true
        fi
        sweep_service "$service"
        # Bootstrap alone starts the job (RunAtLoad); `kickstart -k` here would kill that
        # first incarnation and orphan whatever it had already spawned. Plain kickstart only
        # covers a job that did not start on its own.
        launchctl bootstrap "$DOMAIN" "$(plist_for "$service")"
        launchctl kickstart "$DOMAIN/$(label_for "$service")"
        echo "restarted $service"
    done
}

probe_port() {
    local service="$1" port
    port="$(port_for "$service")"
    case "$service" in
    db)
        nc -z -w2 127.0.0.1 "$port" >/dev/null 2>&1 && echo "port $port: open" || echo "port $port: closed"
        ;;
    *)
        local code
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port$(health_path_for "$service")" 2>/dev/null)" || code="000"
        if [[ "$code" == "000" ]]; then
            echo "port $port: closed"
        else
            echo "port $port: http $code"
        fi
        ;;
    esac
}

cmd_status() {
    for service in $(resolve_services "$@"); do
        echo "== $service =="
        if is_loaded "$service"; then
            launchctl print "$DOMAIN/$(label_for "$service")" | grep -E "state = |pid = |last exit code = " || true
        else
            echo "  not loaded"
        fi
        echo "  $(probe_port "$service")"
    done
}

cmd_logs() {
    local files=()
    for service in $(resolve_services "$@"); do
        files+=("$(log_for "$service")")
    done
    tail -n 40 -f "${files[@]}"
}

main() {
    local action="${1:-}"
    [[ $# -gt 0 ]] && shift
    case "$action" in
    install) cmd_install ;;
    uninstall) cmd_uninstall ;;
    start) cmd_start "$@" ;;
    stop) cmd_stop "$@" ;;
    restart) cmd_restart "$@" ;;
    status) cmd_status "$@" ;;
    logs) cmd_logs "$@" ;;
    *)
        echo "usage: $(basename "$0") install|uninstall|start|stop|restart|status|logs [service ...|all]" >&2
        echo "services: ${ALL_SERVICES[*]}" >&2
        exit 1
        ;;
    esac
}

main "$@"
