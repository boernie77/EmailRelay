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
Internet ──SMTP:25──► Postfix (Hetzner VPS) ──► smtp-proxy (Eingehend)
```

## Infrastruktur

- **Heimserver**: Portainer, DS-Lite (nur IPv6 public), Telekom Glasfaser
- **VPS**: Hetzner CX33 (IPv4+IPv6) — empfängt eingehende Mails, leitet weiter
- **Domains**: Bei Strato, MX-Records zeigen auf Hetzner-VPS
- **Thunderbird**: Nutzt smtp-proxy als SMTP-Server

## Services (Docker Compose)

| Service | Port | Funktion |
|---|---|---|
| `smtp-proxy` | 1587 | SMTP-Proxy für Thunderbird, ersetzt From mit Alias |
| `alias-api` | 8080 | FastAPI: Alias-Verwaltung, Domain-Mgmt, Settings-UI |
| `postgres` | 5432 | Alias-Mappings, Domains, Konfiguration |
| VPS: `postfix` | 25 | Eingehende Mails empfangen + weiterleiten |

## UI-Masken (alias-api Web-Interface)

1. **VPS-Konfiguration**: Host, Port, Credentials für Hetzner-VPS-Verbindung
2. **Domain-Verwaltung**: Eigene Domains hinzufügen/entfernen
3. **E-Mail-Adressen**: Pro Domain festlegen, welche Adressen einen Alias bekommen (nicht automatisch alle)
4. **Alias-Übersicht**: Alle aktiven Aliases, An/Aus, Löschung

## Tech Stack

- **Backend**: Python 3.12, FastAPI, aiosmtpd, SQLAlchemy, asyncpg
- **Frontend**: Jinja2 Templates + TailwindCSS (kein separates JS-Framework)
- **Datenbank**: PostgreSQL 16
- **VPS-Setup**: Postfix + Python-Script für Alias-Lookup via API
- **Container**: Docker Compose, Portainer-kompatibel

## Datenbankschema

```sql
settings       -- VPS-Verbindung, globale Konfiguration
domains        -- Eigene Domains
email_addresses -- Adressen, die Aliases bekommen sollen
aliases        -- alias_address <-> real_address Mapping
```

## Verteilbarkeit / Multi-User

- Die Software ist so aufgebaut, dass sie von beliebigen Personen deployed werden kann
- Alle Zugangsdaten (VPS, Domains, Mail-Adressen) werden ausschließlich über die UI konfiguriert
- Keine hardgecodeten Werte — alles in der Datenbank
- `.env`-Datei nur für DB-Passwort und API-Secret

## Entwicklungsregeln

- Alle Änderungen werden automatisch nach GitHub (boernie77/EmailRelay) gepusht
- CLAUDE.md wird bei größeren Änderungen aktualisiert
- Keine hardgecodierten Zugangsdaten im Code
- Docker-Images müssen ohne Rebuild konfigurierbar sein (Konfiguration via DB/UI)

## GitHub

Repository: https://github.com/boernie77/EmailRelay
