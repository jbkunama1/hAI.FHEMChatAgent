# hAI.FHEMChatAgent

Deutschsprachiger Web-Chat und Telegram-Bot für FHEM. OpenAI-kompatibles Tool Calling, MCP und optional direkte FHEMWEB-Befehle. Jede Schreibaktion benötigt eine eigene Backend-Freigabe.

## Architektur

Docker startet `app:app` mit genau einem Worker. `app.py` verdrahtet die Dienste und die authentifizierten API-Routen:

- `agent_service.py`: Planung, Batch-Abweisung, Chat und Einzelfreigaben.
- `guard.py`: SQLite-Verläufe, Freigaben und persistente Recovery-Sperren.
- `device_service.py`: Geräteliste, Normalisierung und 30-Sekunden-Cache.
- `diagnostics_service.py`: MCP-Discovery und frischer Geräte-Lesezugriff teilen dieselbe Toolliste pro Diagnose. LLM-Modellliste wird separat geprüft.
- `web_routes.py`: regulärer Router für HTML, Geräte, Menü und Diagnose.
- `telegram_service.py`: private Chats mit User-ID-Whitelist, Geräteauswahl und Zustandsversand.
- `index.html`: gemeinsame Weboberfläche; keine eingebettete HTML-Kopie.

`ui_app.py` und `diagnostic_app.py` bleiben reine Import-Aliasse für bestehende Startbefehle. Keine Routen werden nachträglich ersetzt; kein FunctionType-Monkeypatching.

## Sicherheits- und Zustandsmodell

Nur die explizite Lese-Allowlist in `guard.py` läuft ohne Freigabe. Andere MCP-Tools mit `fhem_`-Präfix und alle direkten FHEMWEB-Befehle benötigen einen Vorschlag, gebunden an Benutzer, Ziel, Werkzeug und Argumente. Jeder Modell-Batch mit mehr als einem Tool-Call wird vollständig vor der Ausführung abgewiesen; alle Call-IDs erhalten eine Fehlerantwort.

Vorschlag und zugehöriger Verlauf werden gemeinsam gespeichert. `pending` wird bei Freigabe einmalig zu `consumed`, vor dem externen Aufruf. Gleichzeitig entsteht eine persistente Sperre. Erst die atomare Speicherung der Tool-Antwort und des Endzustands macht daraus `completed` oder `uncertain`. Es gibt keine Exactly-once-Garantie über SQLite und das externe Gerät: Ein Absturz nach Geräteausführung, aber vor Ergebnisspeicherung bleibt konservativ unklar und wird nicht wiederholt.

- Abgelehnt: `rejected`, keine Ausführung, Tool-Antwort im Verlauf.
- Abgelaufen: `expired`, auch ohne Button-Klick beim nächsten Zustandszugriff; Frontend aktualisiert nach Ablauf automatisch.
- Neustart: offene Vorschläge werden `cancelled`; alte `consumed`-Einträge werden konservativ `uncertain`.
- Fehler, Timeout, MCP-Fehlerantwort oder Abbruch einer Schreibausführung: Recovery-Sperre, keine automatische Neuplanung und kein Fallback.
- Nach erfolgreichem Schreiben darf die anschließende Modellrunde nur lesen.

Bei Recovery bleibt Lesen möglich. Prüfe zunächst den tatsächlichen Gerätestatus. Erst `/weiter <neuer Auftrag>` hebt die Sperre bewusst auf; die neue Schreibaktion benötigt wieder eine eigene Freigabe. Ein neuer Chat löscht die Recovery-Sperre nicht. Die Statusprüfung wird nicht technisch nachgewiesen: `/weiter` ist eine ausdrückliche Benutzerbestätigung.

## Start und Konfiguration

```sh
git clone https://github.com/jbkunama1/hAI.FHEMChatAgent.git
cd hAI.FHEMChatAgent
cp .env.example .env
# .env bearbeiten, keine Schlüssel committen
chmod 600 .env
docker compose up -d --build
```

Für Portainer: `portainer-stack.yml` verwenden und den erfolgreichen Image-Build abwarten. Öffentlich nur hinter HTTPS-Reverse-Proxy oder VPN betreiben.

| Variable | Standard / Bedeutung |
|---|---|
| `APP_USERNAME`, `APP_PASSWORD` | `daniel`, Passwort erforderlich |
| `DATA_DIR` | `/data`, dauerhaftes SQLite-Volume |
| `OPENAI_BASE_URL`, `OPENAI_MODEL` | `https://9router.arbeitermili.eu/v1`, `all_coding` |
| `OPENAI_API_KEY` | Für KI-Anfragen erforderlich |
| `MCP_SERVER_URL`, `MCP_API_KEY` | Explizit konfigurieren, keine eingebaute MCP-Zieladresse |
| `MCP_TRANSPORT` | `streamable-http` oder `sse` |
| `MCP_AUTH_HEADER`, `MCP_AUTH_SCHEME` | `Authorization`, `Bearer`; für X-API-Key Scheme leer setzen |
| `FHEM_URL` | Leer deaktiviert direkte Befehle; sonst URL inklusive `/fhem` |
| `FHEM_USERNAME`, `FHEM_PASSWORD` | Optionale FHEMWEB-Basic-Auth |
| `FHEM_VERIFY_TLS` | `true` |
| `APPROVAL_TTL_SECONDS` | `120`, mindestens 10 Sekunden |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` | Bot und kommagetrennte Whitelist; leer deaktiviert Polling |
| `APP_IMAGE`, `APP_PORT` | Deployment-Variablen für Image und Hostport |

FHEMWEB-CSRF wird nicht automatisch behandelt. HTTP-Erfolg allein bestätigt keinen Geräteerfolg. Tooldaten gelten als untrusted Daten.

## Diagnose und Bedienung

Web-Gerätesuche oder Telegram `/devices Wohnzimmer` finden reale Namen. Auswahl erzeugt eine Statusabfrage, keine Schreibfreigabe. Die Weboberfläche zeigt strukturierte Fehler mit `phase` und `code`, etwa `configuration / missing_mcp_url` oder `fhem_connection / connection_refused`.

`/health` ist nur Liveness: ein erfolgreicher HTTP-Aufruf bestätigt weder MCP noch FHEM noch LLM. Die explizite Verbindungsdiagnose prüft eine frische MCP-Geräteliste statt eines Cache-Erfolgs. Die LLM-Prüfung liest nur `/models`; sie beweist kein funktionierendes Tool Calling. Direkte FHEMWEB-Kommandos werden nicht zur Diagnose ausgeführt.

## Migration und Deployment

Vor dem ersten Start dieses Images den bestehenden Container stoppen und das gesamte Datenvolume sichern. SQLite erhält additiv die Tabelle `recovery`; bestehende `chats` und `actions` bleiben erhalten. Alte `consumed`-Datensätze können auch historisch erfolgreiche Aktionen enthalten: Da sie nicht von unterbrochenen Aktionen unterscheidbar sind, kann einmalig eine Recovery-Sperre entstehen. Gerätestatus prüfen und anschließend ausdrücklich `/weiter <neuer Auftrag>` verwenden.

Kein Parallelbetrieb mehrerer App-Prozesse oder Worker auf derselben Datenbank. Erst nach geprüftem CI-Lauf und Volume-Backup das neue Image in Portainer bereitstellen. Dieses Refactoring behebt keine externe MCP-zu-FHEM-Verbindungsstörung.

## Tests und Grenzen

```sh
python -m unittest discover -s tests -v
python -m compileall -q *.py tests
```

Neue Regressionstests decken Batch-Abweisung, Schreibfehler, Abbruch, Ablauf ohne Klick und Neustart zwischen Ausführung und Speicherung ab. HTTP-Routentests und Änderungen am Build-Workflow sind nicht Teil dieses Integrations-Commits. Ein lokaler Unit-Test ist kein CI-, Container- oder Live-FHEM-Nachweis. Live-MCP, FHEM, LLM und Telegram separat in der Zielumgebung prüfen.
