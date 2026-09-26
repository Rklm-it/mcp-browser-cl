#!/usr/bin/env bash
# =============================================================================
# mcp-browser — коннектор для Claude: поиск, любые сайты, браузер, соцсети.
#
# Одной командой на VPS под root:
#   bash <(curl -fsSL --connect-timeout 15 \
#     https://raw.githubusercontent.com/Rklm-it/mcp-browser-cl/main/install.sh)
#
# Рядом с хабом nexus-mcp встаёт на его домен и его Caddy: путь /browser/…,
# свой секрет, свой пользователь. Без хаба — свой Caddy: домен <IP>.sslip.io
# (или --domain), порт 443, а если он занят — 9443.
# Прочее: [--domain d] [--port p] [--no-caddy] [--no-browser] [--branch b]
# [--token GH_TOKEN] (приватный форк). Из скачанной копии: bash install.sh.
#
# Что делает: код в /opt/mcp-browser/app, venv, Chromium (Playwright) с
# библиотеками и шрифтами, пользователь mcp-browser, /etc/mcp-browser.env,
# systemd-юнит mcp-browser (127.0.0.1:8767), маршрут в Caddy.
# =============================================================================
{
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[x]${NC} $*" >&2; exit 1; }

FINISHED=0
on_exit() {
    local rc=$?
    [ "$FINISHED" = "1" ] || echo -e "${RED}[x] Установка оборвалась (код $rc) — коннектор НЕ готов. Вывод выше называет шаг.${NC}" >&2
}
trap on_exit EXIT
export GIT_TERMINAL_PROMPT=0

DOMAIN=""; PORT="443"; PORT_SET=0; WITH_CADDY=1; WITH_BROWSER=1
REPO_URL="https://github.com/Rklm-it/mcp-browser-cl.git"; BRANCH="main"; GH_TOKEN="${GH_TOKEN:-}"
BASE=/opt/mcp-browser; APP=$BASE/app; ENVF=/etc/mcp-browser.env; STATE=/var/lib/mcp-browser
SVC_USER=mcp-browser; LPORT=8767
HUB_CADDY=/etc/caddy-nexus-mcp; HUB_ENV=/etc/nexus-mcp.env

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)     DOMAIN="$2"; shift 2 ;;
        --port)       PORT="$2"; PORT_SET=1; shift 2 ;;
        --no-caddy)   WITH_CADDY=0; shift ;;
        --no-browser) WITH_BROWSER=0; shift ;;
        --repo)       REPO_URL="$2"; shift 2 ;;
        --branch)     BRANCH="$2"; shift 2 ;;
        --token)      GH_TOKEN="$2"; shift 2 ;;
        -h|--help)    [ -f "${BASH_SOURCE[0]:-}" ] && sed -n 2,19p "${BASH_SOURCE[0]}"; FINISHED=1; exit 0 ;;
        *) die "неизвестный параметр: $1" ;;
    esac
done

[ "$(id -u)" = "0" ] || die "нужен root"
envget() { grep -E "^$1=" "$2" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
port_busy() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

# ── 1. Пакеты ────────────────────────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
# apt и Playwright — только с </dev/null: timeout уводит команду в свою группу
# процессов, и apt, тронув терминал, получает SIGTTOU и замирает (статус T в
# ps) — установка «висела» на пакетах, уже поставив их.
APT=(-o DPkg::Lock::Timeout=600 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)
# dpkg занят (на свежем VPS — автообновления): говорим, кого ждём, а не
# молчим — иначе ожидание неотличимо от зависания. Проверяем сам замок dpkg,
# а не имена процессов: unattended-upgrade-shutdown висит в Ubuntu всегда и
# dpkg не держит — по имени он давал ложное «занято».
dpkg_locked() {
    command -v python3 >/dev/null 2>&1 || return 1
    python3 - <<'PY' 2>/dev/null
import fcntl, sys
for p in ("/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock"):
    try:
        f = open(p, "a")
    except OSError:
        continue
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)  # замок держит другой процесс
sys.exit(1)
PY
}
dpkg_holder() {
    pgrep -af '(^|/)(apt|apt-get|dpkg|unattended-upgrade)( |$)' 2>/dev/null | grep -v -- '--wait-for-signal' \
        | head -1 | cut -d' ' -f2- | cut -c1-120
}
waited=0
while dpkg_locked && [ "$waited" -lt 900 ]; do
    [ "$waited" = "0" ] && warn "dpkg занят: $(dpkg_holder || echo 'другой процесс') — жду (обычно автообновления; ускорить: systemctl stop unattended-upgrades)"
    sleep 10; waited=$((waited + 10))
    [ $((waited % 60)) = 0 ] && echo "    …жду dpkg $((waited / 60)) мин"
done
# Только недостающее: на машине с хабом всё уже стоит, apt не нужен вовсе.
NEED=()
for p in git python3 python3-venv curl ca-certificates; do
    dpkg -s "$p" >/dev/null 2>&1 || NEED+=("$p")
done
# Шрифты: без них кириллица и эмодзи на скриншотах — квадраты. Не критично.
FONTS=()
for p in fonts-dejavu-core fonts-noto-color-emoji; do
    dpkg -s "$p" >/dev/null 2>&1 || FONTS+=("$p")
done
if [ ${#NEED[@]} -gt 0 ] || [ ${#FONTS[@]} -gt 0 ]; then
    log "Пакеты: ${NEED[*]} ${FONTS[*]} (apt-get update, до 5 минут)"
    timeout 300 apt-get "${APT[@]}" update -qq </dev/null 2>&1 | tail -3 || warn "apt-get update не прошёл — пробуем с тем, что есть"
    if [ ${#NEED[@]} -gt 0 ]; then
        timeout 900 apt-get "${APT[@]}" install -y --no-install-recommends "${NEED[@]}" </dev/null 2>&1 \
            | { grep -E --line-buffered '^(Get:|Setting up|E:)' || true; } \
            || die "apt-get install не прошёл (или занят dpkg: ps aux | grep -E 'apt|dpkg')"
        for p in "${NEED[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || die "пакет $p не встал — вывод apt выше"; done
    fi
    if [ ${#FONTS[@]} -gt 0 ]; then
        timeout 600 apt-get "${APT[@]}" install -y --no-install-recommends "${FONTS[@]}" </dev/null 2>&1 \
            | { grep -E --line-buffered '^(Get:|E:)' || true; } \
            || warn "шрифты не встали — на скриншотах могут быть квадраты вместо эмодзи"
    fi
else
    log "Пакеты уже стоят"
fi

# ── 2. Код ───────────────────────────────────────────────────────────────────
mkdir -p "$BASE"
SELF_DIR=""
[ -f "${BASH_SOURCE[0]:-}" ] && SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
AUTH_URL="$REPO_URL"
[ -n "$GH_TOKEN" ] && AUTH_URL="$(printf '%s' "$REPO_URL" | sed "s#https://#https://x-access-token:${GH_TOKEN}@#")"
if [ -n "$SELF_DIR" ] && [ -d "$SELF_DIR/mcp_browser" ] && [ "$SELF_DIR" != "$APP" ]; then
    log "Беру код из $SELF_DIR"
    rm -rf "$APP" && cp -a "$SELF_DIR" "$APP"
elif [ -d "$APP/.git" ]; then
    log "Обновляю код"
    timeout 300 git -C "$APP" fetch -q --depth 1 "$AUTH_URL" "$BRANCH" && git -C "$APP" reset -q --hard FETCH_HEAD \
        || die "git fetch не прошёл: доступ к GitHub с этой машины? (приватный форк — --token)"
else
    log "Клонирую код"
    timeout 300 git clone -q --depth 1 -b "$BRANCH" "$AUTH_URL" "$APP" \
        || die "git clone не прошёл: доступ к GitHub с этой машины? (приватный форк — --token)"
    git -C "$APP" remote set-url origin "$REPO_URL"
fi
chmod -R a+rX "$APP"

log "venv и зависимости"
if [ ! -x "$BASE/venv/bin/pip" ]; then
    PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    timeout 600 apt-get "${APT[@]}" install -y -qq "python${PYV}-venv" </dev/null >/dev/null 2>&1 || true
    rm -rf "$BASE/venv"
    python3 -m venv "$BASE/venv" || die "python3 -m venv не прошёл — apt-get install python${PYV}-venv"
fi
PIP=(--timeout 30 --retries 3 --progress-bar off --disable-pip-version-check)
timeout 180 "$BASE/venv/bin/pip" install -q "${PIP[@]}" --upgrade pip >/dev/null 2>&1 || true
if ! timeout 900 "$BASE/venv/bin/pip" install "${PIP[@]}" -r "$APP/requirements.txt" 2>&1 \
        | { grep -E --line-buffered '^(Collecting|Successfully|ERROR)' || true; }; then
    die "pip install не прошёл: доступ к pypi.org с этой машины?"
fi
( cd "$APP" && "$BASE/venv/bin/python" -c "import mcp_browser.server" ) || die "код не импортируется — лог pip выше"

# ── 3. Chromium ──────────────────────────────────────────────────────────────
BROWSER_ON=0
if [ "$WITH_BROWSER" = "1" ]; then
    log "Chromium и его библиотеки (Playwright, ~200 МБ; несколько минут)"
    if PLAYWRIGHT_BROWSERS_PATH="$BASE/pw-browsers" timeout 1500 "$BASE/venv/bin/python" -m playwright \
            install --with-deps chromium </dev/null 2>&1 \
            | { grep -E --line-buffered -i '^(Downloading|Chromium|Installing|Get:|Setting up|E:|Error|Failed)' || true; }; then
        chmod -R a+rX "$BASE/pw-browsers" 2>/dev/null || true
        BROWSER_ON=1
    else
        warn "Chromium не встал — поиск и чтение сайтов работают, браузера нет. Повторите установку позже"
    fi
fi

# ── 4. Пользователь и настройки ──────────────────────────────────────────────
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$STATE/profiles"
chown -R "$SVC_USER:$SVC_USER" "$STATE"
chmod 700 "$STATE" "$STATE/profiles"

# Рядом с хабом — его домен и его Caddy.
HUB=0
if [ "$WITH_CADDY" = "1" ] && [ -f "$HUB_CADDY/Caddyfile" ] && systemctl is-enabled nexus-mcp-caddy >/dev/null 2>&1; then
    HUB=1
    [ -n "$DOMAIN" ] || DOMAIN="$(envget NEXUS_MCP_PUBLIC_HOSTS "$HUB_ENV")"
    PORT="$(envget NEXUS_MCP_PUBLIC_PORT "$HUB_ENV")"; [ -n "$PORT" ] || PORT=443
    log "Нашёл хаб nexus-mcp — встаю на его домен $DOMAIN, путь /browser/"
fi
[ -n "$DOMAIN" ] || DOMAIN="$(envget MCP_BROWSER_PUBLIC_HOSTS "$ENVF")"
if [ "$PORT_SET" = "0" ] && [ "$HUB" = "0" ] && [ -n "$(envget MCP_BROWSER_PUBLIC_PORT "$ENVF")" ]; then
    PORT="$(envget MCP_BROWSER_PUBLIC_PORT "$ENVF")"; PORT_SET=1
fi
if [ "$WITH_CADDY" = "1" ] && [ "$HUB" = "0" ]; then
    if [ -z "$DOMAIN" ]; then
        PUBIP="$(curl -fsS -4 --connect-timeout 5 -m 10 https://api.ipify.org 2>/dev/null || true)"
        [[ "$PUBIP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "не узнал внешний IP — укажите --domain"
        DOMAIN="${PUBIP//./-}.sslip.io"
        log "Домен не задан — беру $DOMAIN (sslip.io отвечает этим IP, DNS настраивать не нужно)"
    fi
    if [ "$PORT_SET" = "0" ] && port_busy 443 && ! ss -ltnpH 'sport = :443' | grep -q caddy; then
        PORT=9443; warn "443 занят — коннектор встанет на $PORT"
    fi
fi

gen() { head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "$1"; }
keep() { local v; v="$(envget "$1" "$ENVF")"; [ -n "$v" ] && printf '%s' "$v" || printf '%s' "${2:-}"; }
SECRET="$(keep MCP_BROWSER_SECRET "$(gen 40)")"
[ -f "$ENVF" ] && cp -a "$ENVF" "$ENVF.bak"
log "Пишу $ENVF"
umask 077
cat > "$ENVF" <<EOF
# mcp-browser. После правки: systemctl restart mcp-browser
MCP_BROWSER_SECRET=$SECRET
MCP_BROWSER_HOST=127.0.0.1
MCP_BROWSER_PORT=$LPORT
MCP_BROWSER_PUBLIC_HOSTS=$DOMAIN
MCP_BROWSER_PUBLIC_PORT=$PORT
MCP_BROWSER_BEHIND_HUB=$HUB
MCP_BROWSER_STATE_DIR=$STATE
MCP_BROWSER_BROWSER=$BROWSER_ON
# Выход в интернет через прокси (http://… или socks5://…), например через ноду.
MCP_BROWSER_PROXY=$(keep MCP_BROWSER_PROXY)
# Поиск: свой SearXNG или ключ Brave Search API. Пусто — DuckDuckGo/Bing/… без ключей.
MCP_BROWSER_SEARXNG_URL=$(keep MCP_BROWSER_SEARXNG_URL)
MCP_BROWSER_BRAVE_KEY=$(keep MCP_BROWSER_BRAVE_KEY)
# Telegram: токен бота (@BotFather) и каналы через запятую (@name или -100…). Бот — админ канала.
MCP_BROWSER_TG_BOT_TOKEN=$(keep MCP_BROWSER_TG_BOT_TOKEN)
MCP_BROWSER_TG_CHATS=$(keep MCP_BROWSER_TG_CHATS)
# VK: ключ пользователя (права wall, photos, offline) и стены через запятую (-id сообщества).
MCP_BROWSER_VK_TOKEN=$(keep MCP_BROWSER_VK_TOKEN)
MCP_BROWSER_VK_OWNERS=$(keep MCP_BROWSER_VK_OWNERS)
MCP_BROWSER_MAX_SESSIONS=$(keep MCP_BROWSER_MAX_SESSIONS 4)
MCP_BROWSER_IDLE_MIN=$(keep MCP_BROWSER_IDLE_MIN 15)
EOF
umask 022

# ── 5. systemd ───────────────────────────────────────────────────────────────
log "systemd-юнит mcp-browser"
cat > /etc/systemd/system/mcp-browser.service <<EOF
[Unit]
Description=mcp-browser — поиск, сайты, браузер и соцсети для Claude
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
EnvironmentFile=$ENVF
Environment=HOME=$STATE
Environment=PLAYWRIGHT_BROWSERS_PATH=$BASE/pw-browsers
WorkingDirectory=$APP
ExecStart=$BASE/venv/bin/python -m mcp_browser
Restart=always
RestartSec=3
# Браузер открывает чужие сайты: ему не видно ничего, кроме своего каталога.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=$STATE
InaccessiblePaths=-/etc/nexus-mcp -/etc/nexus-mcp.env -/var/lib/nexus-mcp
MemoryMax=2G
TasksMax=1024

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable mcp-browser >/dev/null 2>&1
systemctl restart mcp-browser
ok_local=0
for _ in $(seq 10); do
    curl -fsS --connect-timeout 3 -m 5 "http://127.0.0.1:$LPORT/healthz" >/dev/null 2>&1 && { ok_local=1; break; }
    sleep 1
done
if [ "$ok_local" = "1" ]; then
    log "коннектор отвечает на 127.0.0.1:$LPORT"
else
    journalctl -u mcp-browser -n 30 --no-pager >&2 || true
    die "коннектор не поднялся — лог выше"
fi

# ── 6. Caddy ─────────────────────────────────────────────────────────────────
SITE_BLOCK='handle /browser/* {
    reverse_proxy 127.0.0.1:'"$LPORT"' {
        flush_interval -1
    }
}'
if [ "$HUB" = "1" ]; then
    # Caddy хаба: маршрут — файлом в sites/, а в Caddyfile — import (новые
    # версии установщика хаба пишут его сами; старым — дописываем).
    mkdir -p "$HUB_CADDY/sites"
    printf '# mcp-browser (ставит install.sh mcp-browser-cl)\n%s\n' "$SITE_BLOCK" > "$HUB_CADDY/sites/mcp-browser.caddy"
    if ! grep -q "import $HUB_CADDY/sites/\*.caddy" "$HUB_CADDY/Caddyfile"; then
        cp -a "$HUB_CADDY/Caddyfile" "$HUB_CADDY/Caddyfile.bak"
        awk -v imp="    import $HUB_CADDY/sites/*.caddy" '
            { print }
            /^[^ {#][^{]*:[0-9]+ \{$/ && !done { print imp; done=1 }' "$HUB_CADDY/Caddyfile.bak" > "$HUB_CADDY/Caddyfile"
    fi
    CADDY_BIN="$(systemctl show -p ExecStart --value nexus-mcp-caddy | sed -n 's/.*path=\([^ ;]*\).*/\1/p')"
    [ -x "$CADDY_BIN" ] || CADDY_BIN="$(command -v caddy || true)"
    if [ -n "$CADDY_BIN" ] && ! "$CADDY_BIN" validate --config "$HUB_CADDY/Caddyfile" --adapter caddyfile >/dev/null 2>&1; then
        [ -f "$HUB_CADDY/Caddyfile.bak" ] && cp -a "$HUB_CADDY/Caddyfile.bak" "$HUB_CADDY/Caddyfile"
        rm -f "$HUB_CADDY/sites/mcp-browser.caddy"
        die "Caddyfile хаба с маршрутом /browser/ не прошёл проверку — вернул как было. Проверка: $CADDY_BIN validate --config $HUB_CADDY/Caddyfile --adapter caddyfile"
    fi
    systemctl restart nexus-mcp-caddy
    # Чат хаба (приложение Nexus Admin) получает инструменты браузера: ему
    # нужен секрет — drop-in к его юниту, сам юнит не трогаем.
    if systemctl cat nexus-chat >/dev/null 2>&1; then
        mkdir -p /etc/systemd/system/nexus-chat.service.d
        printf '[Service]\nEnvironmentFile=-%s\n' "$ENVF" > /etc/systemd/system/nexus-chat.service.d/mcp-browser.conf
        systemctl daemon-reload
        systemctl try-restart nexus-chat || true
        log "чат хаба видит браузер (инструменты web)"
    fi
elif [ "$WITH_CADDY" = "1" ]; then
    if port_busy 80 && ! ss -ltnpH 'sport = :80' | grep -q "pid=$(systemctl show -p MainPID --value mcp-browser-caddy 2>/dev/null)," ; then
        die "порт 80 занят ($(ss -ltnpH 'sport = :80' | head -1)) — Let's Encrypt не выдаст сертификат. Освободите 80 или --no-caddy"
    fi
    if ! command -v caddy >/dev/null 2>&1; then
        log "Ставлю Caddy"
        ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
        curl -fsSL --connect-timeout 15 -m 180 -o /usr/local/bin/caddy \
            "https://caddyserver.com/api/download?os=linux&arch=$ARCH" || die "Caddy не скачался — поставьте вручную или --no-caddy"
        chmod +x /usr/local/bin/caddy
    fi
    mkdir -p /etc/caddy-mcp-browser
    cat > /etc/caddy-mcp-browser/Caddyfile <<EOF
{
    http_port 80
    https_port $PORT
}
$DOMAIN:$PORT {
    handle {
        reverse_proxy 127.0.0.1:$LPORT {
            flush_interval -1
        }
    }
}
EOF
    cat > /etc/systemd/system/mcp-browser-caddy.service <<EOF
[Unit]
Description=Caddy для mcp-browser
After=network-online.target mcp-browser.service

[Service]
ExecStart=$(command -v caddy) run --config /etc/caddy-mcp-browser/Caddyfile --adapter caddyfile
Restart=always
RestartSec=5
Environment=XDG_DATA_HOME=/var/lib/mcp-browser-caddy

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable mcp-browser-caddy >/dev/null 2>&1
    systemctl restart mcp-browser-caddy
fi

if [ "$WITH_CADDY" = "1" ]; then
    log "Жду https на $DOMAIN (сертификат — до 90 секунд)"
    URLBASE="https://$DOMAIN"; [ "$PORT" = "443" ] || URLBASE="https://$DOMAIN:$PORT"
    [ "$HUB" = "1" ] && URLBASE="$URLBASE/browser"
    for _ in $(seq 18); do
        curl -fsS --connect-timeout 5 -m 10 "$URLBASE/healthz" >/dev/null 2>&1 && break
        sleep 5
    done
fi

install -m 0755 "$APP/bin/mcp-browser" /usr/local/bin/mcp-browser
echo
echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  mcp-browser установлен${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}"
/usr/local/bin/mcp-browser info || true
FINISHED=1
}
