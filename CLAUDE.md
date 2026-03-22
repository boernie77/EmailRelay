# EmailRelay

## Projektübersicht

Self-hosted E-Mail-Alias-Dienst (vergleichbar mit Firefox Relay / SimpleLogin), der als Docker-Container auf Portainer läuft. Entwickler: Christian (GitHub: boernie77).

## Ziel

Thunderbird-Mails werden vor dem Senden abgefangen und die Absenderadresse durch einen zufälligen Alias ersetzt. Eingehende Mails an diesen Alias werden ans echte Postfach weitergeleitet.

## Architektur

```
Thunderbird ──SMTP:1587──► smtp-proxy ──► echter SMTP-Server
                                │
                          alias-api (FastAPI)
                                │
                          PostgreSQL (Alias-Mappings)
                                │
Internet ──SMTP:25──► Postfix (Hetzner VPS) ──► Heimserver weiterleitung
```

## Infrastruktur (produktiv)

- **Heimserver**: Portainer, DS-Lite — IP/Zugangsdaten nur lokal in `.env`
- **VPS**: Hetzner, Ubuntu — Postfix läuft, Port 25 offen — IP nur lokal in `.env`
- **Alias-Domain**: konfiguriert in UI — MX zeigt auf VPS
- **Docker-Stack**: liegt in `/tmp/EmailRelay` auf Heimserver (aus GitHub geklont)
- **Portainer**: Stack `emailrelay` via Git-Repository (URL mit `.git` Suffix nötig!)
- **Thunderbird**: konfiguriert (SMTP-Proxy Port 1587)

## Services (Docker Compose)

| Service | Port | Funktion |
|---|---|---|
| `smtp-proxy` | 1587 | SMTP-Proxy für Thunderbird, ersetzt From mit Alias |
| `alias-api` | 8080 | FastAPI: Alias-Verwaltung, Domain-Mgmt, Settings-UI |
| `postgres` | 5432 | Alias-Mappings, Domains, Konfiguration |
| VPS: `postfix` | 25 | Eingehende Mails empfangen + weiterleiten |

## UI konfiguriert (Stand 2026-03-22)

- Einstellungen: SMTP, Alias-Domain und VPS-IP in der UI konfiguriert
- Domain und Adressen: in der UI angelegt

## Deployment (Updates einspielen)

```bash
cd /tmp/EmailRelay && git pull && docker compose build --no-cache alias-api && docker compose up -d alias-api
```

## VPS — wichtige Dateien

- Postfix config: `/etc/postfix/main.cf`
- Forward-Script: `/usr/local/bin/emailrelay-forward.py`
- API-Secret im Script: `REDACTED_SECRET`

## Tech Stack

- **Backend**: Python 3.12, FastAPI, aiosmtpd, SQLAlchemy, asyncpg
- **Frontend**: Jinja2 Templates + TailwindCSS + Google Fonts (Dancing Script)
- **Datenbank**: PostgreSQL 16
- **VPS-Setup**: Postfix + Python-Script für Alias-Lookup via API
- **Container**: Docker Compose, Portainer-kompatibel

## Offene Todos

- [ ] Thunderbird konfigurieren (SMTP auf Port 1587, Heimserver-IP)
- [ ] VPS-Auto-Setup via SSH implementieren (SSH-Key in UI → automatische Postfix-Konfiguration)
- [ ] Auto-Push Hook funktioniert erst nach Claude-Neustart

## Entwicklungsregeln

- Auto-Push nach GitHub nach jedem Edit/Write (Hook in .claude/settings.json)
- Bei Updates: `git pull` + `docker compose build --no-cache alias-api` auf Heimserver
- Keine hardgecodierten Zugangsdaten im Code
- Screenshots/Bilder nie committen (in .gitignore)

## GitHub

Repository: https://github.com/boernie77/EmailRelay
