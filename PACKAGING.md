# Hörwerk: portable Buildvorbereitung

Dieser Stand bereitet einen **nativen** Ein-Ordner-Build vor; er belegt kein
fertiges Binärpaket. Linux x86_64 wird auf Linux x86_64 und Windows x86_64 auf
Windows x86_64 gebaut. Host und Architektur werden geprüft; Cross-Compilation
wird abgewiesen.

## Lokales Staging

Backends werden weder heruntergeladen noch ausgeführt. Sie müssen ausführbar,
mit Version, öffentlicher Quellen-URL, SHA-256 und Lizenzdatei vorliegen:

```json
{"backends":[
 {"name":"audible","os":"linux","arch":"x86_64","version":"PRÜFEN","source":"https://quelle.example/audible","sha256":"64_HEXZEICHEN","file":"vendor/audible","license":"legal/audible-LICENSE.txt"},
 {"name":"ffmpeg","os":"linux","arch":"x86_64","version":"PRÜFEN","source":"https://quelle.example/ffmpeg","sha256":"64_HEXZEICHEN","file":"vendor/ffmpeg","license":"legal/ffmpeg-LICENSE.txt"}
]}
```

Staging bleibt vollständig symlinkfrei. Die PyInstaller-Ausgabe darf dagegen die
seit PyInstaller 6 auf POSIX üblichen internen Symlinks enthalten. Erlaubt werden
nur relative Dateialiasse, deren strikt aufgelöstes reguläres Ziel im selben
frischen Buildbaum liegt. Externe, absolute, gebrochene, zyklische,
Verzeichnis- und sensitive Aliasse werden abgewiesen. Beim Kopieren werden
erlaubte Aliasse zu normalen Dateien materialisiert. Das Ergebnis und ZIP sind
somit symlinkfrei und FAT-tauglich; doppelte Dateiinhalte benötigen mehr Platz.
Dies ist keine pauschale Freigabe beliebiger Symlinks.

## Befehle und Prüfgrenze

```console
python3 -m unittest discover -s apps/hoerwerk -p 'test_*.py' -v
python3 apps/hoerwerk/entry.py --self-check
python3 apps/hoerwerk/build_portable.py /absolut/staging /absolut/ausgabe --source-sha "$(git rev-parse HEAD)"
```

PyInstaller wird nicht installiert. Der Builder nutzt `shell=False`, isolierte
Dist-/Work-/Spec-Pfade und `--onedir --console`. Vor Veröffentlichung startet er
nur das neu gebaute App-Executable mit exakt `--self-check`, liest dessen JSON
und verlangt `import_ok: true`. Dieser Importtest lädt auch Tkinter und GUI, baut
aber kein Fenster auf und startet weder Vendorbackends noch Kontozugriffe. Ein
grüner Test belegt daher ausdrücklich keinen Display-, USB-, Backend- oder
Kontotest. Fehlende Tcl/Tk-Bibliotheken werden nicht automatisch beschafft,
sondern blockieren die Veröffentlichung.

Das Paket enthält `bin/`, `licenses/`, ein leeres `data/`, Build- und Hashmanifest.
Ziele werden exklusiv ohne Überschreiben veröffentlicht; bei Fehlern wird nur
eine nachweislich eigene Reservierung bereinigt. Es gibt keinen Upload oder
Release-Schritt.

## Noch offene reale Abnahme

Die automatisierten Tests verwenden einen Fake-Builder. Offen bleiben echte
PyInstaller-Kompilationen auf den Zielsystemen, Tk-Fensterstart,
Backend-Lauffähigkeit, AAXC/MPEG/`libmp3lame`, Zorin/glibc, Windows,
USB/FAT/exFAT, Signaturen, Vendor-Lizenzen und Quellcodepflichten. Anmeldung,
Download, Konvertierung und Ende-zu-Ende-Test sind ebenfalls offen.
# Unabhängiger Prüfstand 17.11.2026

Mika: 63 Tests bestanden (52 aktuelle plus 10 erhaltene Regressionstests und
ein zusätzlicher Typprüfungsfall). JSON-Arrays/null und `import_ok: 1` werden
als BuildError abgewiesen. Kein zusätzlicher Codexlauf war dafür erforderlich.

Ein echter Linux-x86_64-Bau mit Python 3.12.14, PyInstaller 6.22.3 und Hooks
2026.10 wurde auf glibc 2.41 durchgeführt. Zunächst fehlten im Bundle Tcl/Tk-
Bibliotheken; der neue native Selbsttest blockierte korrekt und hinterließ
keinen fertigen Zielordner und kein ZIP. Die Bibliotheken waren lokal vorhanden
unter `/opt/codex/runtimes/codex-primary-runtime/dependencies/python/lib`.
Mit diesem Verzeichnis als LD_LIBRARY_PATH ausschließlich in den PyInstaller-
Unterprozessen gelang der Bau. Das erzeugte Executable bestand anschließend
`--self-check` aus /tmp ohne LD_LIBRARY_PATH und PYTHONPATH (Exit 0).

Das temporäre Test-ZIP enthielt ausdrücklich synthetische Vendor-Platzhalter,
die nicht ausgeführt wurden. Es wurde entfernt und ist kein benutzbares
Hörwerk-Distributionspaket. Der Quell-SHA-Eintrag bezeichnete den damaligen
veröffentlichten Ausgangsstand; die geprüften lokalen Verpackungskorrekturen
waren dabei noch unveröffentlicht. Diese Probe belegt Kompilation, Paketaufbau
und Importfähigkeit auf diesem Host, keine Reproduzierbarkeit eines Releases.
Reale portable audible-/FFmpeg-Binärpakete samt Lizenzen, Windows, Zorin auf
älterer glibc, Display, USB und echte Konten/Medien bleiben ungeprüft.

