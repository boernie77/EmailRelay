# E-Mail Relay

Self-hosted E-Mail-Alias-Dienst — vergleichbar mit [Firefox Relay](https://relay.firefox.com/) oder [SimpleLogin](https://simplelogin.io/), aber vollständig unter eigener Kontrolle.

## Was macht das?

Wenn du eine E-Mail versendest, wird deine echte Absenderadresse automatisch durch eine zufällige Alias-Adresse ersetzt (z.B. `xk3mz9apqr@alias.deine-domain.de`). Antworten gehen an den Alias und werden an dein echtes Postfach weitergeleitet — ohne dass der Empfänger deine echte Adresse kennt.

```
Thunderbird ──SMTP:1587──► smtp-proxy ──► echter SMTP-Server
                                │               (z.B. Strato)
                          alias-api (FastAPI)
                                │
                          PostgreSQL
                                │
Internet ──SMTP:25──► Postfix (VPS) ──► dein echtes Postfach
```

## Features

- **Alias beim Senden**: From-Adresse wird automatisch ersetzt, kein manuelles Anlegen nötig
- **Weiterleitung eingehender Mails**: Antworten an den Alias landen in deinem Postfach
- **Web-UI**: Aliases, Domains und Adressen verwalten, Einstellungen konfigurieren
- **VPS Auto-Setup**: Postfix auf dem Mailserver per Knopfdruck via SSH einrichten
- **Docker-basiert**: Läuft als Docker Compose Stack, kompatibel mit Portainer

## Voraussetzungen

- **Heimserver**: Docker / Portainer
- **VPS mit Port 25**: z.B. Hetzner (für eingehende Mails)
- **Domain**: mit MX-Record auf den VPS

## Installation

### 1. Repository klonen

```bash
git clone https://github.com/boernie77/EmailRelay.git
cd EmailRelay
```

### 2. Umgebungsvariablen konfigurieren

```bash
cp .env.example .env
# .env bearbeiten: POSTGRES_PASSWORD und API_SECRET setzen
```

### 3. Starten

```bash
docker compose up -d
```

Die Web-UI ist danach unter `http://localhost:8080` erreichbar.

### 4. Einstellungen in der UI

1. **Einstellungen**: SMTP-Server, Alias-Domain eintragen
2. **Domains**: Deine Absender-Domain hinzufügen
3. **Adressen**: E-Mail-Adressen, die Aliases bekommen sollen
4. **VPS Setup**: SSH-Zugangsdaten eintragen → "VPS Setup ausführen"

### 5. Thunderbird konfigurieren

Neuen Postausgangsserver anlegen:
- Server: `IP-des-Heimservers`
- Port: `1587`
- Verbindungssicherheit: Keine
- Authentifizierung: Keine

## Tech Stack

- **Backend**: Python 3.12, FastAPI, aiosmtpd, SQLAlchemy, asyncpg
- **Frontend**: Jinja2, TailwindCSS
- **Datenbank**: PostgreSQL 16
- **VPS**: Postfix + Python-Forwarding-Script
- **Container**: Docker Compose

## Lizenz

MIT
