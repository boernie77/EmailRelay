# E-Mail Relay

Self-hosted E-Mail-Alias-Dienst — vergleichbar mit [Firefox Relay](https://relay.firefox.com/) oder [SimpleLogin](https://simplelogin.io/), aber vollständig unter eigener Kontrolle und kostenlos.

## Was macht das?

Wenn du eine E-Mail versendest, wird deine echte Absenderadresse automatisch durch eine zufällige Alias-Adresse ersetzt (z.B. `xk3mz9apqr@alias.deine-domain.de`). Antworten gehen an den Alias und werden an dein echtes Postfach weitergeleitet — ohne dass der Empfänger deine echte Adresse je gesehen hat.

```
Thunderbird ──SMTP:1587──► smtp-proxy ──► echter SMTP-Server (z.B. Strato)
                                │
                          alias-api (FastAPI)
                                │
                          PostgreSQL (Alias-Mappings)
                                │
Internet ──SMTP:25──► Postfix (VPS) ──Tailscale VPN──► dein echtes Postfach
```

## Features

- **Automatischer Alias beim Senden**: From-Adresse wird beim ersten Senden automatisch ersetzt, kein manuelles Anlegen nötig
- **Weiterleitung eingehender Mails**: Antworten an den Alias landen im echten Postfach
- **Web-UI**: Aliases, Domains und Adressen verwalten, Einstellungen konfigurieren
- **Chrome Extension**: Alias-Adressen direkt beim Surfen erstellen und ins Formularfeld einfügen
- **VPS Auto-Setup**: Postfix auf dem Mailserver per Knopfdruck via SSH einrichten
- **Docker-basiert**: Läuft als Docker Compose Stack, kompatibel mit Portainer
- **Tailscale-Integration**: Zuverlässige Verbindung zwischen VPS und Heimserver, auch hinter DS-Lite

## Voraussetzungen

### Eigene Domain (Pflicht)
E-Mail Relay funktioniert **nur mit eigenen Domains**. Du brauchst eine Domain (z.B. `deine-domain.de`), für die du einen Subdomain-MX-Record setzen kannst (z.B. `alias.deine-domain.de` → VPS). Dienste wie Gmail, GMX oder Outlook funktionieren als **Ziel** für Weiterleitungen, können aber nicht als Alias-Domain verwendet werden.

### Externer VPS mit Port 25 (Pflicht)
Um E-Mails empfangen zu können, wird ein Server mit **geöffnetem Port 25** benötigt. Die meisten Heimanschlüsse (Telekom, Vodafone, etc.) blockieren Port 25. Ein günstiger VPS (z.B. [Hetzner CX22](https://www.hetzner.com/), ~4 €/Monat) reicht vollständig aus. Wichtig: Nicht alle VPS-Anbieter öffnen Port 25 standardmäßig — bei Hetzner muss Port 25 explizit freigeschaltet werden.

> **Ausgehendes Port 25 ist bei Hetzner (und vielen anderen Anbietern) gesperrt.** Das ist kein Problem: Das Forward-Script sendet direkt über Port 587 deines SMTP-Anbieters (z.B. Strato) und umgeht so die Sperre vollständig.

### Tailscale VPN (Pflicht bei DS-Lite / blockiertem IPv6)
Viele Heimanschlüsse (besonders Telekom Glasfaser) nutzen DS-Lite — dabei fehlt eine native IPv4-Adresse, und viele Router (z.B. Speedport) blockieren eingehende IPv6-Verbindungen komplett. Ohne erreichbare IP kann der VPS die Alias-API auf deinem Heimserver nicht abfragen.

**Lösung:** Installiere [Tailscale](https://tailscale.com/) auf Heimserver und VPS. Tailscale weist stabilen private IPs (100.x.x.x) zu, die sich nie ändern — unabhängig von Router-Modell, IPv6-Konfiguration oder täglichen IP-Wechseln.

```bash
# Auf Heimserver und VPS:
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
systemctl enable tailscaled
```

Nach der Installation zeigt `tailscale ip -4` die stabile IP (z.B. `100.x.x.x`). Diese IP als API-URL in der Alias-Domain-Konfiguration eintragen.

### Heimserver oder lokaler Rechner
Für die Docker-Dienste (smtp-proxy, alias-api, PostgreSQL). Läuft auch auf einem Raspberry Pi oder NAS.

### E-Mail-Konto mit SMTP-Zugang
Zum Weiterleiten ausgehender Mails (z.B. Strato, IONOS, jeder Anbieter mit SMTP-Zugang).

## Installation

### 1. Repository klonen

```bash
git clone https://github.com/boernie77/EmailRelay.git
cd EmailRelay
```

### 2. Umgebungsvariablen konfigurieren

```bash
cp .env.example .env
```

`.env` bearbeiten und folgende Werte setzen:

| Variable | Beschreibung |
|---|---|
| `POSTGRES_PASSWORD` | Beliebiges sicheres Datenbankpasswort |
| `API_SECRET` | Beliebiger geheimer Token (z.B. mit `openssl rand -base64 24`) |

### 3. Starten

```bash
docker compose up -d
```

Die Web-UI ist danach unter `http://localhost:8080` erreichbar.

### 4. Einstellungen in der Web-UI

In der Web-UI unter **Alias-Domains** eine neue Konfiguration anlegen:

1. **Alias-Domain**: Die Subdomain, unter der Aliases vergeben werden (z.B. `alias.deine-domain.de`)
2. **SMTP-Verbindung**: Zugangsdaten deines ausgehenden Mailservers (z.B. Strato)
3. **VPS-Verbindung**: IP, SSH-Benutzer, SSH-Key und die API-URL, über die der VPS die API erreicht — bei Tailscale: `http://100.x.x.x:8080`
4. **VPS Setup ausführen**: Installiert und konfiguriert Postfix automatisch per SSH

### 5. DNS konfigurieren

```
MX  alias.deine-domain.de  →  IP-des-VPS  (Priorität 10)
A   alias.deine-domain.de  →  IP-des-VPS
```

### 6. Domains und Adressen anlegen

In der Web-UI:
- **Domains** (`/domains`): Deine Absender-Domain eintragen (z.B. `deine-domain.de`)
- **Adressen** (`/addresses`): E-Mail-Adressen, die beim Senden einen Alias bekommen sollen

### 7. Thunderbird konfigurieren

Neuen Postausgangsserver anlegen (*Kontoeinstellungen → Postausgangsserver (SMTP) → Hinzufügen*):

| Einstellung | Wert |
|---|---|
| Server | IP des Heimservers |
| Port | `1587` |
| Verbindungssicherheit | Keine |
| Authentifizierung | Keine |

Das Konto im Seitenbaum anklicken → Postausgangsserver auf den neuen Server umstellen.

> **Wichtig:** In den Thunderbird-Kontoeinstellungen kein Reply-To setzen — sonst würde deine echte Adresse beim Antworten sichtbar. Der smtp-proxy entfernt den Reply-To-Header automatisch.

Ab jetzt: Beim ersten Senden von einer konfigurierten Adresse wird automatisch ein Alias erstellt und die From-Adresse ersetzt.

## Fehlerbehebung

### Eingehende Mails kommen nicht an

1. Postfix-Status prüfen: `systemctl status postfix`
2. Mail-Logs prüfen: `tail -50 /var/log/mail.log`
3. VPS → Heimserver-Verbindung testen: `curl http://<TAILSCALE-IP>:8080/api/settings/smtp`
4. Tailscale-Status: `tailscale status` (beide Maschinen müssen verbunden sein)

### "API-Fehler: timed out" im Mail-Log

Der VPS kann den Heimserver nicht erreichen. Typische Ursachen:
- DS-Lite-Anschluss mit blockiertem IPv6 (z.B. Speedport-Router)
- IPv6-Adresse hat sich geändert

**Lösung:** Tailscale auf Heimserver und VPS installieren, in der Alias-Domain-Konfiguration die Tailscale-IP als API-URL eintragen.

### "550 Die verwendete From: Adresse gehört nicht zu Ihrem Paket" (Strato)

Strato prüft, ob die From-Adresse mit dem SMTP-Konto übereinstimmt. Das Forward-Script setzt die From-Adresse automatisch auf den SMTP-Benutzer und speichert die Originaladresse im Reply-To. Wenn der Fehler auftritt: VPS Setup erneut ausführen, um das Script zu aktualisieren.

### "554 SMTP protocol violation: A header line must be terminated by CRLF"

Das Forward-Script auf dem VPS ist veraltet. VPS Setup erneut ausführen.

### Postfix startet nicht

Syntaxfehler in `/etc/postfix/main.cf` prüfen:
```bash
postfix check
journalctl -u postfix -n 50
```

Häufige Ursache: Führende Leerzeichen in main.cf (werden als Fortsetzungszeilen interpretiert):
```bash
sed -i 's/^[[:space:]]*//' /etc/postfix/main.cf
postfix reload
```

### Echte Adresse erscheint beim Antworten

In Thunderbird unter *Kontoeinstellungen → [Konto] → Identitäten* prüfen, ob ein Reply-To eingetragen ist — diesen löschen.

## Chrome Extension

Die Chrome Extension ermöglicht es, direkt beim Ausfüllen von Webformularen einen neuen Alias zu erstellen — ohne die Web-UI öffnen zu müssen.

### Was sie macht

- Klick auf das Extension-Icon öffnet ein Popup
- Alias-Domain und Zieladresse (deine echte Inbox) auswählen
- Bezeichnung eingeben (z.B. der Website-Name)
- Alias wird erstellt, automatisch in die Zwischenablage kopiert und ins zuletzt fokussierte Eingabefeld eingefügt

### Voraussetzungen

- E-Mail Relay muss laufen und über eine **öffentlich erreichbare URL** verfügbar sein (z.B. via Reverse Proxy auf dem VPS, z.B. `https://api.deine-domain.de`)
- In den Extension-Einstellungen: API-URL, API-Secret sowie optional Benutzername und Passwort eintragen
- Mit Benutzerkonto werden nur die eigenen Adressen angezeigt

### Installation (manuell, ohne Chrome Web Store)

```bash
# 1. Repository klonen (oder nur den chrome-extension/-Ordner herunterladen)
git clone https://github.com/boernie77/EmailRelay.git
cd EmailRelay/chrome-extension

# 2. Icons generieren (einmalig, benötigt Python 3)
python3 generate_icons.py
```

3. Chrome öffnen → `chrome://extensions` → **Entwicklermodus** aktivieren (oben rechts)
4. **"Entpackte Erweiterung laden"** → Ordner `chrome-extension` auswählen
5. Extension-Icon in der Toolbar anklicken → **Einstellungen** → API-URL und API-Secret eintragen

### Öffentliche API-URL einrichten

Die Extension muss die API über HTTPS erreichen können. Empfohlene Lösung: Caddy als Reverse Proxy auf dem VPS:

```
# /etc/caddy/Caddyfile
api.deine-domain.de {
    reverse_proxy http://<TAILSCALE-IP-HEIMSERVER>:8080
}
```

```bash
systemctl reload caddy
```

DNS: `A api.deine-domain.de → IP-des-VPS`

## Tech Stack

- **Backend**: Python 3.12, FastAPI, aiosmtpd, SQLAlchemy, asyncpg
- **Frontend**: Jinja2, TailwindCSS
- **Datenbank**: PostgreSQL 16
- **VPS**: Postfix + Python-Forwarding-Script (smtplib)
- **VPN**: Tailscale (für zuverlässige VPS↔Heimserver-Verbindung)
- **SSH-Automatisierung**: paramiko
- **Container**: Docker Compose

## Lizenz

Copyright (c) 2026 Christian. Alle Rechte vorbehalten.

Die Nutzung, Vervielfältigung oder Verbreitung dieses Projekts oder von Teilen davon ist ohne ausdrückliche schriftliche Genehmigung des Urhebers nicht gestattet.
