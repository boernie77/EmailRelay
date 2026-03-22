#!/bin/bash
# EmailRelay VPS Setup-Script
# Ausführen auf dem Hetzner-VPS als root:
#   curl -s https://raw.githubusercontent.com/boernie77/EmailRelay/main/vps-setup/setup.sh | bash
# Oder: bash setup.sh

set -e

echo "=== EmailRelay VPS Setup ==="
echo ""
read -p "Alias-Domain (z.B. alias.deine-domain.de): " ALIAS_DOMAIN
read -p "EmailRelay API-URL (z.B. http://DEINE_HEIMSERVER_IPv6:8080): " API_URL
read -p "API-Secret (gleicher Wert wie in .env): " API_SECRET

echo ""
echo "Installiere Abhängigkeiten..."
apt-get update -qq
apt-get install -y -qq postfix python3 python3-pip curl

pip3 install -q httpx aiosmtplib 2>/dev/null || pip3 install --break-system-packages -q httpx aiosmtplib

echo ""
echo "Konfiguriere Postfix..."

# Backup
cp /etc/postfix/main.cf /etc/postfix/main.cf.bak 2>/dev/null || true

cat > /etc/postfix/main.cf << EOF
# EmailRelay Postfix Konfiguration
smtpd_banner = \$myhostname ESMTP
biff = no
append_dot_mydomain = no
readme_directory = no

myhostname = $(hostname -f)
mydomain = $(hostname -d 2>/dev/null || echo "localhost")
myorigin = \$myhostname
inet_interfaces = all
inet_protocols = all

# Lokale Zustellung nur für Postmaster
mydestination = localhost

# Relay für Alias-Domain
virtual_mailbox_domains = ${ALIAS_DOMAIN}
virtual_transport = emailrelay

# Alle Mails für Alias-Domain an unser Script
virtual_mailbox_maps = regexp:/etc/postfix/virtual_mailbox_regex

# TLS
smtpd_tls_security_level = may
smtp_tls_security_level = may

# Limits
message_size_limit = 52428800
EOF

# Alle Adressen der Alias-Domain akzeptieren
cat > /etc/postfix/virtual_mailbox_regex << EOF
/@${ALIAS_DOMAIN}$/  OK
EOF

# Transport-Definition
cat >> /etc/postfix/master.cf << EOF

# EmailRelay forwarder
emailrelay unix  -       n       n       -       -       pipe
  flags=Rq user=nobody argv=/usr/local/bin/emailrelay-forward.py \${recipient}
EOF

# Forward-Script installieren
cat > /usr/local/bin/emailrelay-forward.py << PYEOF
#!/usr/bin/env python3
"""Postfix pipe: Leitet eingehende Mails an echte Adressen weiter."""
import sys
import os
import email
import httpx
import smtplib
from email.parser import BytesParser
from email import policy

API_URL = "${API_URL}"
API_SECRET = "${API_SECRET}"

def resolve_alias(alias_address):
    resp = httpx.get(
        f"{API_URL}/api/alias/incoming/{alias_address}",
        headers={"x-api-secret": API_SECRET},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["real_address"]

def main():
    if len(sys.argv) < 2:
        sys.exit(1)

    alias_address = sys.argv[1].lower()
    raw = sys.stdin.buffer.read()

    try:
        real_address = resolve_alias(alias_address)
    except Exception as e:
        print(f"API-Fehler: {e}", file=sys.stderr)
        sys.exit(75)  # EX_TEMPFAIL – Postfix versucht es später nochmal

    if not real_address:
        print(f"Alias {alias_address} nicht gefunden", file=sys.stderr)
        sys.exit(67)  # EX_NOUSER

    # Mail parsen und To/Cc ersetzen
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    del msg["To"]
    msg["To"] = real_address

    # Weiterleitung via lokalen Postfix
    with smtplib.SMTP("localhost", 25) as smtp:
        smtp.sendmail("noreply@${ALIAS_DOMAIN}", [real_address], msg.as_bytes())

    print(f"Weitergeleitet: {alias_address} -> {real_address}")

if __name__ == "__main__":
    main()
PYEOF

chmod +x /usr/local/bin/emailrelay-forward.py

postmap /etc/postfix/virtual_mailbox_regex
postfix reload

echo ""
echo "=== Setup abgeschlossen ==="
echo ""
echo "Nächste Schritte:"
echo "1. DNS: MX-Record für ${ALIAS_DOMAIN} → $(curl -s ifconfig.me) setzen"
echo "2. DNS: A-Record für ${ALIAS_DOMAIN} → $(curl -s ifconfig.me) setzen"
echo "3. Firewall: Port 25 öffnen: ufw allow 25/tcp"
echo "4. Test: echo 'Test' | mail -s 'Test' test@${ALIAS_DOMAIN}"
echo ""
echo "Postfix-Status: systemctl status postfix"
