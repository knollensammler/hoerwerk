# Hörwerk: deutsche Bedienoberfläche

## Quellstart

Erforderlich sind Python 3.11+ und Python mit Tkinter. Vom Repository-Stamm:

    python3 apps/hoerwerk/gui.py

Der App-Pfad stammt aus `gui.py`, nicht aus dem Arbeitsverzeichnis. Eingefrorene
Pakete werden zur Laufzeit abgelehnt, solange kein eigener Login-Einstiegspunkt
existiert. Auch `pythonw.exe` ist für den Terminallogin nicht geeignet.

## Funktionen und Grenzen

Die GUI aktualisiert die echte lokale Bibliothek und führt Download und Export
als jeweils einen abbrechbaren Hintergrundjob aus. Kontopfade, ASIN und lokale
Audiodateien werden vor Benutzung auf Querprofile und Symlinks geprüft. AAXC gilt
nur mit passendem, gewährtem Voucher als vollständig. Partielle Ergebnisse werden
nicht gelöscht; ein erneuter Download darf sie reparieren. Ein Backend-Skip gilt
nur dann als Erfolg, wenn danach genau ein vollständiges Ergebnis nachweisbar ist.

Die Anmeldung läuft nur mit Gnome Terminal (`--wait`), Konsole (`--nofork`) oder
Xterm, deren gestarteter Prozess synchron überwacht werden kann. Ein generischer
Terminalserver wird nicht als kontrollierbar behauptet. Bei Gnome/Konsole muss
die Anmeldung im separaten Terminal beendet werden; ein GUI-Abbruch bleibt bis
zum Ende des wartenden Terminalclients offen und meldet vorher keinen Abschluss.
Direkt kontrollierte Prozesse nutzen bei Abbruch begrenztes
Wait sowie nötigenfalls Kill und ein abschließendes Wait. Bei der Frage nach dem
externen Browser ist **Ja** zu wählen; das Amazon-Kennwort gehört nur in den
Browser. Der Status bedeutet ausschließlich „lokal eingerichtet, nicht online
geprüft“.

Vor AAXC-Export weist die GUI auf kurzzeitig in lokalen FFmpeg-Argumenten
sichtbare Schlüssel hin. Bestehende Ziele werden nicht überschrieben.

## Tests und offene Abnahme

    python3 -m unittest discover -s apps/hoerwerk -p 'test_*.py' -v

Die Tests sind headless und verwenden Fake-Prozesse. Nicht geprüft sind echte
Anmeldung, Konto, Download oder Konvertierung, realer FFmpeg-/Audible-Build,
Displaylauf, Windows, physischer USB-Datenträger und portable Pakete. Backends,
Tk und Lizenztexte sind noch nicht gebündelt; eine fertige App wird nicht behauptet.
