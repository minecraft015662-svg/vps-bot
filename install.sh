#!/usr/bin/env bash
# ============================================================
#  VPS BOT installer  -  by Sarthak34
#  Run:  bash <(curl -fsSL https://raw.githubusercontent.com/YOUR_USER/YOUR_REPO/main/install.sh)
#  (root se chalao)
# ============================================================

# >>> GitHub pe upload karke YOUR_USER / YOUR_REPO badlo (ya REPO_RAW env se do) <<<
REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/YOUR_USER/YOUR_REPO/main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/vps-bot}"
SERVICE="vpsbot"
LOG="/tmp/vpsbot-install.log"

AQUA=$'\033[1;96m'; GREEN=$'\033[1;92m'; RED=$'\033[1;91m'; YELLOW=$'\033[1;93m'
DIM=$'\033[2m'; BOLD=$'\033[1m'; NC=$'\033[0m'

SRC_DIR=""
case "$0" in /dev/*|bash|-bash|sh) ;; *) [ -f "$0" ] && SRC_DIR="$(cd "$(dirname "$0")" && pwd)" ;; esac

# ---------- input (curl | bash me bhi chale) ----------
TTY_DEV="${TTY_DEV:-/dev/tty}"
if ( : <"$TTY_DEV" ) 2>/dev/null; then exec 3<"$TTY_DEV"; else exec 3<&0; fi

ask()        { local v; printf '%s' "$2" >&2; IFS= read -r -u 3 v || v=""; printf -v "$1" '%s' "$v"; }
ask_secret() { local v; printf '%s' "$2" >&2; IFS= read -r -s -u 3 v || v=""; echo >&2; printf -v "$1" '%s' "$v"; }
pause()      { local _; printf '\n  %sEnter dabao menu pe wapas jaane ke liye...%s' "$DIM" "$NC" >&2; IFS= read -r -u 3 _ || true; }

ok()   { printf '  %s✔%s %s\n' "$GREEN" "$NC" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$NC" "$*"; }
err()  { printf '  %s✘%s %s\n' "$RED" "$NC" "$*"; }

banner() {
  [ -t 1 ] && clear
  while IFS= read -r l; do printf '%s%s%s\n' "$AQUA" "$l" "$NC"; done <<'BANNER_EOF'
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                                                ┃
┃                  ◆ VPS BOT ◆                   ┃
┃              VPS BOT BY SARTHAK34              ┃
┃                   Sᴀʀᴛʜᴀᴋ34                    ┃
┃                      v3.9                      ┃
┃                                                ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
BANNER_EOF
  echo
}

step() {  # step "message" command...
  local msg="$1"; shift
  printf '  %s%-40s%s' "$BOLD" "$msg" "$NC"
  if "$@" >>"$LOG" 2>&1; then printf '%s✔%s\n' "$GREEN" "$NC"; return 0; fi
  printf '%s✘%s\n' "$RED" "$NC"; tail -n 12 "$LOG" | sed 's/^/      /'; return 1
}

# ---------- environment ----------
is_systemd() { [ -d /run/systemd/system ] && [ "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" = "systemd" ]; }
docker_ok()  { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }
unit_path()  { echo "/etc/systemd/system/${SERVICE}.service"; }

apt_install() {
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
  apt-get update -y -qq && apt-get install -y -qq python3 python3-venv python3-pip curl wget ca-certificates tar
}

docker_works() { timeout 180 docker run --rm hello-world >/dev/null 2>&1; }

install_docker() {
  curl -fsSL https://get.docker.com | sh && systemctl enable --now docker
}

fetch_file() {  # fetch_file name
  local f="$1"
  if [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/$f" ]; then
    [ "$SRC_DIR" = "$INSTALL_DIR" ] || cp "$SRC_DIR/$f" "$INSTALL_DIR/$f"
  else
    case "$REPO_RAW" in *YOUR_USER*) echo "REPO_RAW set nahi hai (install.sh ke upar GitHub URL daalo)"; return 1;; esac
    curl -fsSL "$REPO_RAW/$f" -o "$INSTALL_DIR/$f"
  fi
}

make_venv() {
  cd "$INSTALL_DIR" || return 1
  [ -x venv/bin/python ] || python3 -m venv venv
  ./venv/bin/pip install -q --upgrade pip
  ./venv/bin/pip install -q -r requirements.txt
}

write_env() {
  [ -f "$INSTALL_DIR/.env" ] && cp "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.bak"
  {
    echo "DISCORD_TOKEN=$TOKEN"
    echo "OWNER_IDS=$OWNERS"
    echo "VPS_BACKEND=$BACKEND"
  } > "$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
}

write_runtime() {
  cat > "$INSTALL_DIR/start.sh" <<'S_EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ -f bot.pid ] && kill -0 "$(cat bot.pid)" 2>/dev/null; then echo "Bot already chal raha hai (pid $(cat bot.pid))"; exit 0; fi
nohup bash -c 'while true; do ./venv/bin/python bot.py >> bot.log 2>&1; sleep 5; done' >/dev/null 2>&1 &
echo $! > bot.pid
echo "Bot start ho gaya (pid $(cat bot.pid)).  Logs: tail -f $PWD/bot.log"
S_EOF
  cat > "$INSTALL_DIR/stop.sh" <<'S_EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -f bot.pid ] && kill "$(cat bot.pid)" 2>/dev/null; rm -f bot.pid
pkill -f "$PWD/venv/bin/python bot.py" 2>/dev/null || true
echo "Bot band ho gaya."
S_EOF
  chmod +x "$INSTALL_DIR/start.sh" "$INSTALL_DIR/stop.sh"
}

write_service() {
  local after="network-online.target" req=""
  if [ "$BACKEND" = "docker" ]; then after="network-online.target docker.service"; req="Requires=docker.service"; fi
  cat > "$(unit_path)" <<UNIT
[Unit]
Description=Discord VPS Bot (by Sarthak34)
After=$after
$req

[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload && systemctl enable --now "$SERVICE"
}

# ================= 1) INSTALL =================
install_bot() {
  banner
  printf '  %s>> INSTALL THE BOT%s\n\n' "$BOLD" "$NC"
  [ "$(id -u)" -eq 0 ] || { err "Root se chalao:  sudo bash install.sh"; return 1; }
  command -v apt-get >/dev/null 2>&1 || { err "Sirf Debian/Ubuntu (apt) supported hai."; return 1; }

  # --- token + admin id ---
  TOKEN="${DISCORD_TOKEN:-}"; OWNERS="${OWNER_IDS:-}"
  if [ -z "$TOKEN" ]; then
    while :; do
      ask_secret TOKEN "  Bot Token (type karte waqt dikhega nahi): "
      TOKEN="${TOKEN//[[:space:]]/}"
      [ -n "$TOKEN" ] && break
      err "Token khaali nahi ho sakta."
    done
  fi
  if [ ${#TOKEN} -lt 50 ] || [[ "$TOKEN" != *.* ]]; then
    warn "Token ka format thoda alag lag raha hai (normally lamba hota hai aur '.' hota hai)."
  fi
  if [ -z "$OWNERS" ]; then
    while :; do
      ask OWNERS "  Admin (Owner) Discord ID  [kai ho to comma se]: "
      OWNERS="${OWNERS//[[:space:]]/}"
      [[ "$OWNERS" =~ ^[0-9]+(,[0-9]+)*$ ]] && break
      err "Sirf numbers (Discord User ID) daalo. Developer Mode ON karke profile pe 'Copy User ID'."
    done
  fi
  echo
  ok "Token  : ${TOKEN:0:6}…(hidden)"
  ok "Admin  : $OWNERS"
  echo

  : > "$LOG"
  step "Packages install (python, curl...)" apt_install || return 1

  # --- backend: docker ya local ---
  BACKEND="local"
  if docker_ok; then
    BACKEND="docker"
  elif is_systemd; then
    step "Docker install" install_docker && docker_ok && BACKEND="docker"
  fi
  if [ "$BACKEND" = "docker" ]; then
    printf '  %s%-40s%s' "$BOLD" "Docker test (hello-world)" "$NC"
    if docker_works; then printf '%s✔%s\n' "$GREEN" "$NC"; else printf '%s✘%s\n' "$RED" "$NC"; BACKEND="local"; fi
  fi
  if [ "$BACKEND" = "docker" ]; then
    ok "Backend: DOCKER  (har VPS ek alag container)"
  else
    warn "Backend: LOCAL  (Docker nahi mila / is VPS me chalta nahi - har VPS ek folder + sshx session)"
  fi

  mkdir -p "$INSTALL_DIR"
  step "Bot files (bot.py)"        fetch_file bot.py          || return 1
  step "Bot files (requirements)"  fetch_file requirements.txt || return 1
  step "Python environment (venv)" make_venv                  || return 1
  step ".env likh raha hu"         write_env                  || return 1

  if is_systemd; then
    step "Service banake start kar raha hu" write_service || return 1
    MODE="systemd"
  else
    step "Background runner banaya" write_runtime || return 1
    bash "$INSTALL_DIR/start.sh" >>"$LOG" 2>&1
    MODE="nohup"
  fi

  sleep 3
  echo
  printf '%s' "$GREEN"
  echo "  ┌─────────────────────────────────────────────┐"
  echo "  │   ✔  Bot install ho gaya aur chal raha hai  │"
  echo "  └─────────────────────────────────────────────┘"
  printf '%s\n' "$NC"
  echo "  Install folder : $INSTALL_DIR"
  echo "  Backend        : $BACKEND"
  if [ "$MODE" = "systemd" ]; then
    echo "  Status         : systemctl status $SERVICE --no-pager"
    echo "  Logs           : journalctl -u $SERVICE -f"
    echo "  Restart        : systemctl restart $SERVICE"
  else
    echo "  Start          : bash $INSTALL_DIR/start.sh"
    echo "  Stop           : bash $INSTALL_DIR/stop.sh"
    echo "  Logs           : tail -f $INSTALL_DIR/bot.log"
    warn "Is VPS me systemd nahi hai - VPS restart ho to 'bash $INSTALL_DIR/start.sh' dobara chalao."
  fi
  echo
  echo "  Developer Portal > Bot me ye ON karo: Server Members Intent + Message Content Intent"
  echo "  Bot ko invite karo: scopes 'bot' + 'applications.commands', permission 'Manage Server'."
  return 0
}

# ================= 2) UNINSTALL =================
bot_ids() {
  [ -f "$INSTALL_DIR/vps_db.json" ] || return 0
  python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1])).get("vps",{}).keys()))' "$INSTALL_DIR/vps_db.json" 2>/dev/null \
    | tr ' ' '\n' | grep -E '^[0-9a-f]{12}$' || true
}

uninstall_bot() {
  banner
  printf '  %s>> UNINSTALL THE BOT%s\n\n' "$BOLD" "$NC"
  [ "$(id -u)" -eq 0 ] || { err "Root se chalao:  sudo bash install.sh"; return 1; }
  if [ ! -d "$INSTALL_DIR" ] && [ ! -f "$(unit_path)" ]; then warn "Bot install hi nahi hai ($INSTALL_DIR nahi mila)."; return 0; fi
  warn "Ye bot, uski .env aur vps_db.json (invites, VPS records) hata dega."
  ask CONFIRM "  Pakka uninstall karna hai? ('yes' likho): "
  [ "$CONFIRM" = "yes" ] || { echo "  Cancel kiya."; return 0; }
  echo

  local ids; ids="$(bot_ids)"
  local rmc="n" rmi="n" bk="n"
  if [ -n "$ids" ] && docker_ok; then
    ask rmc "  Bot ke banaye $(echo "$ids" | wc -l) VPS containers bhi delete karun? [y/N]: "
    ask rmi "  Bot ki base images bhi hata dun? [y/N]: "
  fi
  if [ -f "$INSTALL_DIR/vps_db.json" ]; then ask bk "  vps_db.json ka backup /root me rakhu? [Y/n]: "; fi

  : > "$LOG"
  if [ -f "$(unit_path)" ]; then
    step "Service band + hata raha hu" bash -c "systemctl disable --now $SERVICE; rm -f $(unit_path); systemctl daemon-reload" || true
  fi
  [ -x "$INSTALL_DIR/stop.sh" ] && step "Background bot band" bash "$INSTALL_DIR/stop.sh"
  # LOCAL mode ke sshx sessions
  for f in "$INSTALL_DIR"/vps_data/*/.sshx.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; done
  pkill -f "$INSTALL_DIR/bin/sshx" 2>/dev/null || true

  if [[ "$rmc" =~ ^[Yy] ]]; then
    for c in $ids; do docker rm -f "$c" >>"$LOG" 2>&1; done
    ok "VPS containers delete ho gaye."
  fi
  if [[ "$rmi" =~ ^[Yy] ]]; then
    docker images --format '{{.Repository}}:{{.Tag}}' | grep '^sarthak-vps-base' | xargs -r docker rmi -f >>"$LOG" 2>&1
    ok "Base images hat gayi."
  fi
  if [ -f "$INSTALL_DIR/vps_db.json" ] && [[ ! "$bk" =~ ^[Nn] ]]; then
    cp "$INSTALL_DIR/vps_db.json" "/root/vps_db.backup.json" && ok "Backup: /root/vps_db.backup.json"
  fi
  step "Bot files delete" rm -rf "$INSTALL_DIR"
  echo
  printf '%s  ✔ Bot uninstall ho gaya.%s  (Docker aur Python system pe waise hi hain.)\n' "$GREEN" "$NC"
}

# ================= 3) STATUS =================
status_bot() {
  banner
  printf '  %s>> BOT STATUS%s\n\n' "$BOLD" "$NC"
  if [ -f "$(unit_path)" ] && is_systemd; then
    systemctl status "$SERVICE" --no-pager 2>&1 | head -8 | sed 's/^/  /'
    echo; journalctl -u "$SERVICE" -n 12 --no-pager 2>&1 | sed 's/^/  /'
  elif [ -f "$INSTALL_DIR/bot.pid" ] && kill -0 "$(cat "$INSTALL_DIR/bot.pid")" 2>/dev/null; then
    ok "Bot chal raha hai (pid $(cat "$INSTALL_DIR/bot.pid"))"
    echo; tail -n 12 "$INSTALL_DIR/bot.log" 2>/dev/null | sed 's/^/  /'
  elif [ -d "$INSTALL_DIR" ]; then
    warn "Bot install hai par chal nahi raha."
  else
    warn "Bot install nahi hai."
  fi
}

# ================= MENU =================
case "${1:-}" in
  install)   install_bot;   exit $? ;;
  uninstall) uninstall_bot; exit $? ;;
  status)    status_bot;    exit $? ;;
esac

while :; do
  banner
  echo "     ${BOLD}[1]${NC}  Install the bot"
  echo "     ${BOLD}[2]${NC}  Uninstall the bot"
  echo "     ${BOLD}[3]${NC}  Bot status & logs"
  echo "     ${BOLD}[0]${NC}  Exit"
  echo
  ask CH "  Select an option: "
  case "$CH" in
    1) install_bot;   pause ;;
    2) uninstall_bot; pause ;;
    3) status_bot;    pause ;;
    0|q|Q) echo; exit 0 ;;
    *) ;;
  esac
done
