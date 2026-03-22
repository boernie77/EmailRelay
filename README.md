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
Internet ──SMTP:25──► Postfix (VPS) ──► dein echtes Postfach
```

## Features

- **Automatischer Alias beim Senden**: From-Adresse wird beim ersten Senden automatisch ersetzt, kein manuelles Anlegen nötig
- **Weiterleitung eingehender Mails**: Antworten an den Alias landen im echten Postfach
- **Web-UI**: Aliases, Domains und Adressen verwalten, Einstellungen konfigurieren
- **VPS Auto-Setup**: Postfix auf dem Mailserver per Knopfdruck via SSH einrichten
- **Docker-basiert**: Läuft als Docker Compose Stack, kompatibel mit Portainer

## Voraussetzungen

### Eigene Domain (Pflicht)
E-Mail Relay funktioniert **nur mit eigenen Domains**. Du brauchst eine Domain (z.B. `deine-domain.de`), für die du einen Subdomain-MX-Record setzen kannst (z.B. `alias.deine-domain.de` → VPS). Dienste wie Gmail, GMX oder Outlook funktionieren als **Ziel** für Weiterleitungen, können aber nicht als Alias-Domain verwendet werden.

### Externer VPS mit Port 25 (Pflicht)
Um E-Mails empfangen zu können, wird ein Server mit **geöffnetem Port 25** benötigt. Die meisten Heimanschlüsse (Telekom, Vodafone, etc.) blockieren Port 25. Ein günstiger VPS (z.B. [Hetzner CX22](https://www.hetzner.com/), ~4 €/Monat) reicht vollständig aus. Wichtig: Nicht alle VPS-Anbieter öffnen Port 25 standardmäßig — bei Hetzner muss Port 25 explizit freigeschaltet werden.

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

### 4. Einstellungen in der Web-UI (`/settings`)

1. **Alias-Domain**: Die Subdomain, unter der Aliases vergeben werden (z.B. `alias.deine-domain.de`)
2. **SMTP-Verbindung**: Zugangsdaten deines ausgehenden Mailservers
3. **VPS-Verbindung**: IP, SSH-Benutzer, SSH-Key und die URL, über die der VPS die API erreicht (z.B. `http://[IPv6-Adresse]:8080`)
4. **VPS Setup ausführen**: Installiert und konfiguriert Postfix automatisch per SSH

### 5. DNS konfigurieren

Beim VPS-Setup werden die benötigten DNS-Einträge angezeigt. Grundsätzlich benötigt:

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

Ab jetzt: Beim ersten Senden von einer konfigurierten Adresse wird automatisch ein Alias erstellt und die From-Adresse ersetzt.

## Tech Stack

- **Backend**: Python 3.12, FastAPI, aiosmtpd, SQLAlchemy, asyncpg
- **Frontend**: Jinja2, TailwindCSS
- **Datenbank**: PostgreSQL 16
- **VPS**: Postfix + Python-Forwarding-Script
- **SSH-Automatisierung**: paramiko
- **Container**: Docker Compose

## Lizenz

MIT
