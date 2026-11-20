# Usagecast

[![test](https://github.com/Louis-Lastella/usagecast/actions/workflows/test.yml/badge.svg)](https://github.com/Louis-Lastella/usagecast/actions/workflows/test.yml)

[English](README.md) · **Deutsch**

Selbst gehostetes Dashboard, das zeigt, was deine KI-Usage frisst. Im Moment liest es
[Hermes Agent](https://github.com/NousResearch/hermes-agent) mit Anthropic Claude und schlüsselt die Kosten auf: jedes
Tool, jeder Skill, jedes Plugin, jeder Teil des Systemprompts, Denken, Hintergrund-Prüfung, Cache-Neuaufbau nach Pausen
und Cache-Brüche. Dazu kommen die Abo-Limits (5 Stunden, Woche, Extra-Guthaben, für jedes Claude-Konto im Pool von Hermes) mit Prognose, Warnungen aufs Handy und
konkrete Spartipps aus den eigenen Daten, mit dem `hermes`-Befehl, der sie umsetzt.

Nur Python-Standardbibliothek. Es liest `~/.hermes/state.db` und `~/.hermes/logs/agent.log*` nur lesend, auch die jedes Hermes-Profils. Keine KI:
Rangliste, Aufteilung, Prognose und Spartipps sind feste Rechenregeln, es geht nie eine Anfrage an ein Modell raus.

Läuft mit jeder Hermes-Installation. Welche Plugins und welcher Memory-Anbieter aktiv sind, liest es aus `config.yaml`
(`plugins.enabled`, `memory.provider`) und ordnet deren Teile im Systemprompt und an den Nachrichten danach zu.
SOUL.md, Hermes' eigene Hinweise und die Herkunft (Telegram, Discord, Slack, Cron-Jobs ...) erkennt es ebenfalls selbst.

![Übersicht: Wochenlimit mit Tempo, Tagesbudget und Maskottchen, 5-Stunden-Fenster, Extra-Guthaben, darunter ein zweites Konto](docs/overview.png)

Ohne Hermes ausprobieren: `python3 app.py --demo` startet es mit einem Monat erfundener Daten und zwei Konten.

## Seiten

| Seite | Inhalt |
|---|---|
| `/` | Limits pro Konto mit Tempo, Tagesbudget, Prognose und Maskottchen, Gerade jetzt (arbeitende Agenten und ihr Modell), Kennzahlen mit Vergleich zur Vorwoche, Rangliste was am meisten frisst, Bots & Chats (eine Zeile pro Bot, antippen zeigt Themen, Kanäle und Threads), Modelle, Spartipps |
| `/history` | Kosten pro Tag nach den größten Posten mit Markern für Hermes-Updates und Config-Änderungen, diese Woche über den drei davor, Heatmap nach Wochentag und Uhrzeit, Aktivitätskalender, Tagestabelle |
| `/details` | Alle Tools, Skills, Plugins, Systemprompt-Teile und Tool-Beschreibungen einzeln, Cache-Brüche mit wahrscheinlicher Ursache, die zehn teuersten einzelnen Tool-Ergebnisse |
| `/sessions` | Teuerste Sessions mit Bot · Chat, Modell und letzter Aktivität unter jedem Titel, einem Chip pro Bot, Chat-Filter und Suche; der Chip Cron-Jobs zeigt Kosten pro Lauf und Woche. Eine Session-Seite zeigt jeden Schritt, das Modell, die gestarteten Subagenten mit Modell und Kosten und den Befehl `hermes --resume <id>` |
| `/wrapped` | Ein Monat oder ein Jahr als Story-Folien (siehe Wrapped unten) |
| `/projects` | Kosten pro Projekt, antippen zeigt die Sessions dazu |
| `/settings` | Eine kurze Liste mit je einer Seite: Darstellung, Warnungen, Benachrichtigungen, Budget-Wächter, Erweitert |
| `/api/summary` | Limits, Prognose, Konten und Kosten der Limit-Woche als JSON, für Widgets |

Zeitraum per `?p=w` (seit dem letzten Reset des Wochenlimits, Standard), `?p=1`, `?p=7`, `?p=30` oder ein eigener
Zeitraum wie `?p=2026-10-01..2026-10-07` (Knopf „Eigener“).

![Verlauf: Kosten pro Tag nach den größten Posten und Heatmap nach Wochentag und Uhrzeit](docs/history.png)

Hell und dunkel folgen der Systemeinstellung. Auf dem iPhone in Safari öffnen und „Zum Home-Bildschirm“ wählen, dann
startet es wie eine App.

<img src="docs/phone-dark.png" width="260" alt="Übersicht auf dem Handy im Dunkelmodus, mit der Tab-Leiste unten">

**Mehrere Konten:** Liegen im Anmelde-Pool von Hermes mehrere Claude-Konten, bekommt jedes einen eigenen Block, gelesen
mit seiner eigenen Anmeldung. Ein Etikett zeigt, welches gerade in Benutzung ist (Verbrauch oder Anfragen sind in den
letzten 30 Minuten gestiegen), welches Hermes als Nächstes nimmt und welches am Limit steht. Warnungen laufen pro Konto,
der Budget-Wächter greift erst, wenn alle Konten knapp sind. Mit einem Konto sieht alles aus wie vorher.

**Gerade jetzt:** Die Agenten, die in diesem Moment arbeiten, mit Bot · Chat, was sie tun, wie lange schon und mit
welchem Modell, dazu ihre Subagenten. Aktualisiert sich alle 5 Sekunden, solange die Seite offen ist, und warnt, wenn
das Hermes-Gateway nicht läuft.

**Mehrere Konten:** Die Limit-Karte zeigt zuerst alle Konten zusammen als eins (Mittelwert, nächster Reset); "2 Konten"
klappt jedes Konto einzeln auf, und das Gerät merkt sich, ob es offen war.

**Maskottchen:** Neben der großen Wochenzahl sitzt ein animierter Begleiter: Terminal, Debug-Ente, Server-Rack, Kaffee
oder Bot. Er folgt dem 5-Stunden-Fenster (ohne eins der Woche) und hat vier Stimmungen, je nachdem wie voll das Fenster
ist: ruhig, beschäftigt ab 50 %, knapp ab 85 % (oder knappe Prognose) und schlafend am Limit. Kaffeestand und Bot-Akku zeigen, was übrig ist. Er folgt hell und
dunkel und steht still bei reduzierter Bewegung. Auswählen oder ausschalten unter Einstellungen > Darstellung.

## Wrapped

`/wrapped?m=2026-10` (ein Monat) oder `/wrapped?y=2026` (ein Jahr) zeigt deine Nutzung als Story-Folien im Vollbild, per
Tippen oder Wischen: Tokens und API-Gegenwert, das Top-Projekt, das meistgenutzte Tool, die geschäftigste Stunde, die
längste Serie, wie oft du ans Limit gestoßen bist, Subagent-Läufe und ein Nutzungstyp mit dem Maskottchen. Unter jeder
großen Zahl steht eine gedämpfte Zeile im Vergleich zum Monat oder Jahr davor. Ein laufender Monat heißt „bisher“. Der Verlauf
verlinkt jeden Monat und jedes Jahr mit Daten, und in den ersten Tagen eines Monats verweist die Übersicht auf den letzten.
Alles wird lokal aus denselben Daten berechnet.

## Sprachen

Standard ist Englisch. Auf Deutsch schaltest du über den Link unten auf jeder Seite oder mit `?lang=de`, die Wahl bleibt
per Cookie gespeichert. Mit `USAGECAST_LANG=de` ist Deutsch der Standard für Dashboard und Warnungen.

Alle Texte stehen in `locales/<code>.json`. Für eine neue Sprache `locales/en.json` kopieren, die Werte übersetzen und
`python3 app.py --test` laufen lassen. Der Selbsttest prüft, dass jede Sprache dieselben Schlüssel und Platzhalter hat
und jede Seite ohne übrig gebliebene Schlüssel rendert.

## So wird gerechnet

1. **Echte Kosten:** `session_model_usage` enthält pro Session die Tokens, die Anthropic gemeldet hat (Input,
   Cache lesen, Cache schreiben, Output). Mit der offiziellen API-Preisliste (`PRICES` in `app.py`) ergibt das den
   exakten API-Gegenwert. Beim Pro- oder Max-Abo zählt Anthropic das Limit nach denselben Gewichten.
2. **Aufteilung:** Jede Session wird Schritt für Schritt nachgespielt. Für jeden API-Call ist bekannt, was im Prompt
   stand (Systemprompt-Teile, Tool-Beschreibungen, jede Tool-Rückgabe, Plugin-Einblendungen, Nachrichten, Denken),
   was neu dazukam und was aus dem Cache kam.
3. **Cache:** Wo Hermes' `agent.log` den Call hat (`in=… cache=…`), zählt der echte Cache-Wert. So werden auch
   Cache-Brüche ohne Pause sichtbar. Sonst gilt die Regel: nach mehr als `cache_ttl` (5 min oder 1 h) Pause ist der
   Cache weg.
4. **Eichung:** Größen kommen aus der Textlänge (Bilder pauschal) und werden pro Session auf die echten Token-Zahlen
   skaliert. Die Summe aller Posten ist immer exakt gleich den echten Kosten (der Selbsttest prüft das).
5. **Zeitraum:** Kosten zählen nach dem Zeitpunkt jedes einzelnen Schritts. Eine Session, die vor dem Zeitraum begann,
   zählt nur mit dem Teil, der in den Zeitraum fällt.

Denken, das Hermes nicht als Text ablegt, wird aus der Differenz zum echten Output ergänzt und bleibt wie gespeichertes
Denken im Verlauf.

**Projekte:** Hermes speichert nur bei Sessions im Terminal einen Arbeitsordner. Alle anderen Sessions zählen zu dem
Projekt, dessen Pfad in ihren Tool-Aufrufen am häufigsten vorkommt: Ordner unter `/opt` und `/srv` sowie Git-Repos im
Home-Ordner (auch eine Ebene tiefer, etwa `~/projects/app`). Hermes' eigener Ordner zählt nur, wenn sonst kaum etwas
vorkommt.

**Prognose:** Das 5-Stunden-Fenster wird aus dem Tempo der letzten Stunde hochgerechnet. Für die Woche testet Usagecast
an deinen letzten vollen Limit-Wochen zwei Verfahren: eine gerade Linie aus dem Tempo seit dem Reset und deinen
Wochenrhythmus (welcher Anteil der Kosten einer üblichen Woche bis zu dieser Stunde anfällt). Der Rhythmus zählt, wenn er
mindestens 10 % genauer ist; der Ausklapptext unter dem Wochenlimit nennt Verfahren und beide Abweichungen (Ergebnis in
`data/forecast.json`).

**Tempo und Budget:** Ein Strich auf jedem Limit-Balken zeigt, wie viel vom Fenster schon vorbei ist. Liegt die
Prognose über 100 %, heißt es „Zu schnell“ mit der Uhrzeit, zu der das Limit voll ist, bei 85 bis 100 % „Knapp“, darunter
„Im Plan“ mit dem erwarteten Stand zum Reset. Die Linie unter den Limits zeigt die laufende Limit-Woche mit der
100-%-Linie und gestrichelt die Prognose bis zum Reset. Das Tagesbudget ist der Rest der Woche geteilt durch die Tage bis zum Reset. Meldet die
Statusseite von Anthropic eine Störung, steht unter den Limits eine Zeile dazu.

**Aufteilung des Limits:** Aus den Limit-Messungen alle 10 Minuten und den Kosten pro Stunde schätzt Usagecast, wie viel
API-Gegenwert ein Prozent deiner Woche ist. Damit teilt es die Woche in Hermes, Claude Code (Sessions in
`~/.claude/projects` auf demselben Rechner oder von anderen Rechnern gemeldet, siehe unten) und den Rest (claude.ai,
die Apps). Kosten lassen sich statt
in Geld auch als Anteil der Woche anzeigen.

**Preise:** Ein Modell, das in der Preisliste fehlt, bekommt die Preise von Claude Opus und den Hinweis „Preis geschätzt“.

## Einstellungen

`/settings` ist eine kurze Liste, jede Zeile öffnet ihre eigene Seite: **Darstellung** (hell, dunkel oder System,
Sprache, Maskottchen, USD oder EUR zum Tageskurs der EZB, Zahlen kurz oder voll, Standard-Zeitraum, Limits verbraucht oder übrig,
Kosten in Geld oder in % der Woche), **Warnungen** (jede einzeln mit Schwelle, Ruhezeiten), **Benachrichtigungen** (ntfy
oder Telegram, siehe unten), **Budget-Wächter** und **Erweitert** (andere Rechner, welcher Hermes-Ordner gelesen wird,
eigener ntfy-Server, Dashboard-Adresse). Jede Seite speichert nur ihre eigenen Werte. Sie landen in
`data/settings.json`, lesbar nur für den Besitzer; Umgebungsvariablen geben nur die Standardwerte vor. Gespeichert wird
nur, wenn das Formular von Usagecasts eigener Seite kommt. Standardmäßig liest Usagecast den Haupt-Ordner von
Hermes und alle Profile (`hermes profile create`) zusammen; unter Erweitert lässt es sich auf eins beschränken. Die
Limits bleiben gleich, sie gehören zum Konto.

## Warnungen per ntfy

Alle 10 Minuten prüft der Server die Limits und schickt höchstens einmal pro Fenster eine Push-Nachricht, wenn

- das 5-Stunden-Fenster beim Tempo der letzten Stunde bald voll ist (Standard: in 30 Minuten),
- die Wochenprognose über 100 % liegt (frühestens einen Tag nach dem Reset),
- die Woche 80 % und 90 % erreicht,
- das 5-Stunden-Fenster nach einem vollen Fenster wieder frei ist,
- das Extra-Guthaben angezapft wird, und wenn das Guthaben im Monat eine deiner Stufen überschreitet (z. B. 10 und 20,
  standardmäßig aus),
- ein Chat teuer geworden ist: seine letzten drei Antworten kosten je mindestens 0,5 % der Woche (ein neuer Chat liegt
  weit darunter; 0,5 % kosten beim Autor die teuersten 5 % aller Antworten),
- ein einzelner Lauf aus dem Rahmen fällt: ein Cron-Lauf oder eine Antwort kostet das Dreifache des Üblichen und
  mindestens 3 % der Woche.

Die letzten beiden brauchen erst ein paar Tage Limit-Messungen (sie rechnen in % der Woche). Antippen öffnet die Session.

**Budget-Wächter** (standardmäßig aus): Auf `/settings/budget` markierst du wiederkehrende Cron-Jobs, die warten dürfen. Wird
die Woche knapp (Prognose über 100 % ab dem zweiten Tag, Woche bei 90 % oder 5-Stunden-Fenster bei 85 %), pausiert
Usagecast sie mit `hermes cron pause` und setzt sie fort, sobald wieder Luft ist, jeweils mit einem leisen Push. Jobs,
die du selbst pausiert hast, bleiben unangetastet; die Übersicht zeigt, welche Jobs gerade warten.

Am Sonntagabend kommt leise ein Wochenrückblick: Stand der Woche, größter Posten und Vergleich zur Vorwoche.

**Einrichtung in einer Minute:** Auf `/settings/notify` „Eigenes Topic“ wählen, dann erzeugt „Push einrichten“ ein eigenes, zufälliges Topic auf
[ntfy.sh](https://ntfy.sh). Unter Android öffnet ein Knopf die ntfy-App direkt mit dem Abo, auf dem iPhone kopierst du
das Topic und folgst den drei Schritten dort. „Test senden“ zeigt sofort die Antwort des Servers. Ein eigener
ntfy-Server mit Zugangs-Token geht auch, ebenso Hermes' ntfy-Kanal (`NTFY_HOME_CHANNEL`, `NTFY_SERVER_URL`, `NTFY_TOKEN`
in `~/.hermes/.env`). `USAGECAST_NTFY` (volle URL mit Topic) setzt einen Standard ohne die Einstellungsseite.

## Telegram-Mini-App

Mit einem Hermes-Telegram-Bot läuft Usagecast auch in Telegram. Den Menü-Knopf des Bots per BotFather oder mit
`setChatMenuButton` (Typ `web_app`) auf das Dashboard zeigen lassen, unter einer https-Adresse, die das Handy erreicht,
zum Beispiel über Tailscale. Dort geöffnet nutzt es Telegrams Haptik und Zurück-Knopf, übernimmt die Farben, läuft auf
dem Handy im Vollbild und bietet in den Einstellungen „Zum Home-Bildschirm“ an.

Auf `/settings/notify` wählt die Karte Telegram, welches Hermes den Bot stellt (Token und Chat aus dessen `.env`:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_HOME_CHANNEL`), und auf Wunsch ein Thema. Dann gehen Warnungen an Telegram (Push über
Telegram) mit einem Knopf „Öffnen“, der Menü-Knopf zeigt die Limits (`W 16% · 5h 68%`), und eine angepinnte Nachricht
hält Woche, 5-Stunden-Fenster und arbeitende Agenten alle 10 Minuten aktuell. Bei mehreren Konten nennen beide das
aktive.

## Widgets (iPhone, Home- und Sperrbildschirm)

`widget/usagecast-widget.js` ist ein Skript für [Scriptable](https://scriptable.app), das Wochenlimit und
5-Stunden-Fenster des aktiven Kontos zeigt. In Scriptable ein neues Skript anlegen und den Inhalt einfügen, ein kleines Scriptable-Widget
hinzufügen, das Skript wählen und die Dashboard-Adresse als „Parameter“ eintragen. Es liest `/api/summary`, das Handy
muss das Dashboard also erreichen (zum Beispiel über Tailscale).

Dasselbe Skript läuft auf dem Sperrbildschirm (ab iOS 16): Sperrbildschirm lange drücken, „Anpassen“, den
Sperrbildschirm wählen, auf den Widget-Bereich (oder die Zeile über der Uhr) tippen, Scriptable hinzufügen, dann das
neue Widget antippen, das Skript wählen und die Adresse als „Parameter“ eintragen. Rund zeigt die Woche als Ring,
rechteckig die Woche mit Reset, einen dünnen Balken und das 5-Stunden-Fenster, die Zeile über der Uhr „Week 65% · 5h 6%“.
iOS färbt sie passend zur Uhr ein.

## Claude Code auf anderen Rechnern

Claude Code auf einem Laptop nutzt dieselben Limits, schreibt seine Transkripte aber dort. `tools/cc-report.py`
(Standardbibliothek, Python 3.9 oder neuer) summiert die Token-Zahlen pro Stunde und Modell und schickt nur diese Zahlen
alle 10 Minuten ans Dashboard; die Limit-Aufteilung zählt sie dann als Claude Code. Auf `/settings/advanced` unter „Andere
Rechner“ einen Token erstellen und den dort gezeigten Befehl auf dem anderen Rechner ausführen, zum Beispiel

```sh
curl -fsSO https://dashboard.example:8443/cc-report.py
python3 cc-report.py --url https://dashboard.example:8443 --token TOKEN --install
```

Auf dem Mac lädt das einen LaunchAgent (`dev.usagecast.report`, alle 10 Minuten), unter Linux gibt es eine
crontab-Zeile aus. `--dry-run` zeigt, was gesendet würde. Die Einstellungen liegen in `~/.config/usagecast/report.json`
(Rechte 600). Der Endpunkt ist `POST /api/ingest` mit `Authorization: Bearer <token>`; ein neuer Token auf `/settings`
sperrt den alten aus.

## Betrieb

```bash
python3 app.py --test        # Selbsttest
python3 app.py --ntfy-test   # Test-Nachricht an ntfy schicken
python3 app.py --demo        # erfundene Daten, liest nichts aus Hermes, schickt keine Warnungen
PORT=7681 python3 app.py     # Server auf 127.0.0.1:7681
```

| Variable | Bedeutung |
|---|---|
| `PORT` | Port auf 127.0.0.1, Standard 7682 |
| `HERMES_HOME` | Hermes-Ordner, Standard `~/.hermes` |
| `USAGECAST_DATA` | Ablage für Messungen und Limit-Verlauf, Standard `data/` neben `app.py` |
| `USAGECAST_LANG` | Standardsprache, `en` (Standard) oder `de` |
| `USAGECAST_NTFY` | ntfy-Ziel, falls nicht aus Hermes |
| `USAGECAST_URL` | Standard für die Dashboard-Adresse in den Einstellungen, ein Tipp auf die Warnung öffnet sie |

Im Hintergrund misst der Server einmal am Tag Systemprompt und Tool-Beschreibungen (`--snapshot`, läuft im
Hermes-venv) und holt alle 10 Minuten die Abo-Limits über Hermes' OAuth-Login. Der Token verlässt dabei den
Hermes-Prozess nicht. Beides landet in `data/`.

`usagecast.service` ist eine Vorlage für einen systemd-Benutzerdienst mit dem Repo unter `~/usagecast`.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
