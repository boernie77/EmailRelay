#!/bin/bash
# ============================================================
# E-Mail Relay – Installer
# Unterstützt: Ubuntu 22.04 / 24.04
#
# Ausführen auf einem frischen Server als root:
#   curl -fsSL https://raw.githubusercontent.com/boernie77/EmailRelay/main/install.sh | bash
# ============================================================

set -e

# Farben
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${B}  →${NC} $1"; }
success() { echo -e "${G}  ✓${NC} $1"; }
warn()    { echo -e "${Y}  ⚠${NC} $1"; }
error()   { echo -e "${R}  ✗ FEHLER:${NC} $1"; exit 1; }
step()    { echo -e "\n${BOLD}── $1 ──────────────────────────────────────${NC}"; }
ask()     { echo -e "\n${BOLD}$1${NC}"; }

# ── Voraussetzungen prüfen ─────────────────────────────────

if [ "$EUID" -ne 0 ]; then
    error "Bitte als root ausführen:  sudo bash install.sh"
fi

if ! command -v curl &>/dev/null; then
    apt-get update -qq && apt-get install -y -qq curl
fi

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║         E-Mail Relay  –  Installer           ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo "  Dieses Script richtet E-Mail Relay auf diesem Server ein."
echo "  Voraussetzung: Eine (Sub-)Domain zeigt bereits per"
echo "  A-Record auf die IP-Adresse dieses Servers."
echo ""
echo -e "  Server-IP: ${BOLD}$(curl -fsSL ifconfig.me 2>/dev/null || echo 'unbekannt')${NC}"
echo ""

# ── Eingaben ──────────────────────────────────────────────

step "Konfiguration"

ask "1) Web-Domain für das Interface (z.B. relay.meine-domain.de):"
read -r API_DOMAIN
[[ -z "$API_DOMAIN" ]] && error "Domain darf nicht leer sein."

ask "2) Port für den SMTP-Proxy (Thunderbird verbindet sich hier) [Standard: 1587]:"
read -r SMTP_PORT_INPUT
SMTP_PORT=${SMTP_PORT_INPUT:-1587}

ask "3) SMTP-Authentifizierung aktivieren? (empfohlen, da der Port öffentlich erreichbar ist) [J/n]:"
read -r SMTP_AUTH_INPUT
if [[ "$SMTP_AUTH_INPUT" =~ ^[Nn]$ ]]; then
    SMTP_AUTH_REQUIRED=false
    warn "SMTP-Auth deaktiviert. Der SMTP-Proxy ist ohne Passwortschutz erreichbar."
else
    SMTP_AUTH_REQUIRED=true
fi

# Prüfen ob .env schon existiert
ENV_FILE=/opt/EmailRelay/.env
KEEP_ENV=false
if [ -f "$ENV_FILE" ]; then
    echo ""
    warn ".env existiert bereits unter $ENV_FILE"
    ask "Bestehende Zugangsdaten behalten? (empfohlen bei Updates) [J/n]:"
    read -r KEEP_INPUT
    [[ ! "$KEEP_INPUT" =~ ^[Nn]$ ]] && KEEP_ENV=true
fi

if [ "$KEEP_ENV" = false ]; then
    POSTGRES_PASSWORD=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
    API_SECRET=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 48)
fi

# ── Docker installieren ────────────────────────────────────

step "Docker"

if command -v docker &>/dev/null; then
    success "Docker bereits installiert ($(docker --version | cut -d' ' -f3 | tr -d ','))"
else
    info "Installiere Docker..."
    apt-get update -qq
    apt-get install -y -qq ca-certificates gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io
    systemctl enable --now docker
    success "Docker installiert."
fi

# ── docker-compose installieren ────────────────────────────

step "docker-compose"

if command -v docker-compose &>/dev/null; then
    success "docker-compose bereits installiert ($(docker-compose version --short 2>/dev/null || echo 'vorhanden'))"
else
    info "Installiere docker-compose..."
    COMPOSE_URL=$(curl -fsSL https://api.github.com/repos/docker/compose/releases/latest \
        | grep '"browser_download_url"' \
        | grep "docker-compose-linux-x86_64\"" \
        | cut -d'"' -f4)
    if [ -z "$COMPOSE_URL" ]; then
        COMPOSE_URL="https://github.com/docker/compose/releases/download/v5.1.1/docker-compose-linux-x86_64"
    fi
    curl -fsSL "$COMPOSE_URL" -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose
    success "docker-compose installiert."
fi

# ── Caddy installieren ─────────────────────────────────────

step "Caddy (HTTPS / Reverse Proxy)"

if command -v caddy &>/dev/null; then
    success "Caddy bereits installiert."
else
    info "Installiere Caddy..."
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null
    echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] \
https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
    success "Caddy installiert."
fi

# ── Repository klonen / aktualisieren ─────────────────────

step "E-Mail Relay Code"

if [ -d /opt/EmailRelay/.git ]; then
    info "Aktualisiere Repository..."
    cd /opt/EmailRelay && git pull
    success "Repository aktualisiert."
else
    info "Lade Repository herunter..."
    git clone https://github.com/boernie77/EmailRelay /opt/EmailRelay
    success "Repository heruntergeladen."
fi

# ── .env erstellen ────────────────────────────────────────

step ".env Datei"

if [ "$KEEP_ENV" = false ]; then
    info "Erstelle .env..."
    cat > /opt/EmailRelay/.env << EOF
# Datenbank
POSTGRES_DB=emailrelay
POSTGRES_USER=emailrelay
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}

# API-Sicherheit (langer zufälliger String – niemals weitergeben)
API_SECRET=${API_SECRET}

# SMTP-Proxy (Port für Thunderbird)
SMTP_PROXY_PORT=${SMTP_PORT}
SMTP_AUTH_REQUIRED=${SMTP_AUTH_REQUIRED}
EOF
    success ".env erstellt."
else
    # Nur SMTP_AUTH_REQUIRED und Port aktualisieren falls nicht vorhanden
    grep -q "SMTP_PROXY_PORT" "$ENV_FILE" || echo "SMTP_PROXY_PORT=${SMTP_PORT}" >> "$ENV_FILE"
    grep -q "SMTP_AUTH_REQUIRED" "$ENV_FILE" || echo "SMTP_AUTH_REQUIRED=${SMTP_AUTH_REQUIRED}" >> "$ENV_FILE"
    success "Bestehende .env behalten."
fi

# ── Caddy konfigurieren ───────────────────────────────────

step "Caddy Konfiguration"

cat > /etc/caddy/Caddyfile << EOF
${API_DOMAIN} {
    reverse_proxy localhost:8080
}
EOF
systemctl reload caddy || systemctl restart caddy
success "Caddy konfiguriert → https://${API_DOMAIN}"

# ── Systemd-Service einrichten ────────────────────────────

step "Autostart (Systemd)"

cat > /etc/systemd/system/emailrelay.service << EOF
[Unit]
Description=E-Mail Relay
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/EmailRelay
ExecStart=/usr/local/bin/docker-compose up -d
ExecStop=/usr/local/bin/docker-compose down
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable emailrelay
success "Autostart aktiviert (startet automatisch nach Neustart)."

# ── Docker-Stack bauen und starten ────────────────────────

step "E-Mail Relay starten"

info "Baue Docker-Images (dauert 1–2 Minuten beim ersten Mal)..."
cd /opt/EmailRelay
docker-compose up -d --build
success "E-Mail Relay gestartet."

# ── Fertig ────────────────────────────────────────────────

echo ""
echo -e "${G}${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${G}${BOLD}║            Installation abgeschlossen!  ✓            ║${NC}"
echo -e "${G}${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Web-Interface:${NC}  https://${API_DOMAIN}"
echo -e "  ${BOLD}SMTP-Proxy:${NC}     Port ${SMTP_PORT} (Server-IP, für Thunderbird)"
echo ""
echo -e "  ${BOLD}Nächste Schritte:${NC}"
echo -e "  1. https://${API_DOMAIN} öffnen"
echo -e "  2. Admin-Account erstellen (erster Login)"
echo -e "  3. Im Admin-Bereich: VPS-Konfiguration und Alias-Domain einrichten"
echo -e "     (dort wird Postfix für eingehende Mails automatisch konfiguriert)"
echo ""
if [ "$KEEP_ENV" = false ]; then
    echo -e "  ${BOLD}Generierte Zugangsdaten${NC} (gespeichert in /opt/EmailRelay/.env):"
    echo -e "  PostgreSQL-Passwort: ${POSTGRES_PASSWORD}"
    echo -e "  API-Secret:          ${API_SECRET}"
    echo ""
fi
echo -e "  ${Y}Wichtig:${NC} .env-Datei sichern! Sie enthält alle Zugangsdaten."
echo -e "           → cp /opt/EmailRelay/.env ~/emailrelay-env-backup.txt"
echo ""
