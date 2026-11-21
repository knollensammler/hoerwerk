# Hörwerk – portabler Anwendungskern

## Stand und Grenzen

Dieses Arbeitspaket ist **keine fertige Audible-App**. Es enthält einen mit Python
3.11 und der Standardbibliothek testbaren Kern für getrennte Kontoprofile,
Bibliotheksexport, Download und die Konvertierung bereits abspielbarer Audiodateien.
Es enthält weder GUI noch Beispielbibliothek, Zugangsdaten, Netzwerkanmeldung oder
einen vollständigen Audible→MP3-Weg.

Der Kern startet ein lokales `audible-cli` als separaten Prozess. Pro Profil wird
`AUDIBLE_CONFIG_DIR` auf `data/accounts/<Profil-ID>` gesetzt; eine bestehende
`~/.audible`-Konfiguration wird nicht verwendet. Profil-IDs werden kleingeschrieben,
damit Groß-/Kleinschreibungsvarianten auch auf Windows kein Konto vermischen.
Windows-Gerätenamen sind als Profil-IDs gesperrt. `quickstart` ist interaktiv und
die spätere Browseranmeldung ist noch nicht integriert. Amazon-Passwörter soll
Hörwerk weder abfragen noch protokollieren.

Downloads können AAXC plus Voucher oder in einzelnen Fällen MP3 ergeben. AAX und
AAXC werden hier ausdrücklich **nicht** konvertiert: Dafür fehlt noch eine gegen
das reale Voucher-/Entitlement-Schema geprüfte Kette. FFmpeg wird ausschließlich
für schon abspielbare Eingaben verwendet. Vorhandene MP3-Dateien bleiben erhalten.

## MP3-Veröffentlichung und Dateisystemgrenze

Die fertige temporäre MP3 wird bevorzugt per Hardlink atomar und ohne Überschreiben
veröffentlicht. Auf Dateisystemen ohne Hardlinks (beispielsweise FAT/exFAT) wird
der Zielname stattdessen exklusiv reserviert und die Datei hineinkopiert. Dadurch
kann während dieses Fallbacks kurz eine unvollständige Zieldatei sichtbar sein;
ein vollständig atomarer Austausch ist dort ohne Überschreibungsrisiko nicht
möglich. Bei Abbruch wird nur die von Hörwerk selbst reservierte Teildatei entfernt.
Dies gilt für kontrolliert durchgereichte Abbrüche; bei Prozessabsturz oder
Stromausfall kann im Kopierfallback eine Teildatei zurückbleiben.
Eine fremde oder konkurrierend angelegte Zieldatei wird niemals ersetzt oder
gelöscht, und die Originalaudiodatei bleibt stets erhalten. Erwartete Fehler
werden als anwendungsgeeignete Fehler ohne rohe Backendausgaben gemeldet.

## Abhängigkeiten und Startmodell

* Python 3.11+ zum Entwickeln und Testen
* später lokal gebündeltes `audible-cli` 0.6.0 (AGPL-3.0-only)
* später lokal gebündeltes FFmpeg mit `libmp3lame` und dessen Lizenzhinweisen

Erkannte Programme liegen je nach Betriebssystem als `bin/audible[.exe]` und
`bin/ffmpeg[.exe]` (alternativ unter `backend/`) relativ zum App-Verzeichnis.
Tests dürfen Pfade und Prozessadapter explizit injizieren. Es werden keine
Shell-Befehlsstrings verwendet. Die Lizenzen aller gebündelten Bausteine müssen
bei Distribution eingehalten werden. Dieses Dokument trifft **keine**
Lizenzentscheidung für den originalen GsG-Code.

## Tests

Vom Repository-Stamm:

```console
python3 -m unittest discover -s apps/hoerwerk -p 'test_*.py' -v
```

Die Tests verwenden Fake-Prozesse; sie führen keine Anmeldung und keinen echten
Download aus.

## Verbleibende Meilensteine

1. Lokale, interaktive Anmeldung über einen externen Browser sicher integrieren.
2. Voucher-/Entitlement-Schema mit realem Konto zulässig prüfen und eine
   funktionierende AAXC→MP3-Kette implementieren.
3. Eine deutsche GUI für Konto, Bibliothek, Download und Zielauswahl erstellen.
4. Portable Linux- und Windows-Binaries samt Backends und Lizenztexten bauen.
5. Paketierung sowie einen Ende-zu-Ende-Test mit einem echten Nutzerkonto und
   einem rechtmäßig gekauften Titel durchführen.

Das nächste begrenzte Paket sollte Punkt 1 umsetzen und testen, ohne Kennwörter
selbst zu verarbeiten; Punkt 2 darf erst auf Basis eines verifizierten Schemas
als funktionsfähig bezeichnet werden.
