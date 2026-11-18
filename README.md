# Hörwerk

Hörwerk ist eine deutschsprachige Desktop-Anwendung von GsG e. V. für Hörbücher auf Linux und Windows (x86-64).

- Audible-Bibliothek per Gerätecode verbinden, durchsuchen, sortieren und filtern
- Mehrere Titel auswählen und freigegebene Titel parallel herunterladen
- Audio mit Fortschrittsanzeige und Abbruchmöglichkeit exportieren, unter anderem als MP3
- Lokale Dateien abspielen oder Audible-Titel im offiziellen Cloud Player öffnen. Audible entscheidet, ob der vollständige Titel oder eine Hörprobe verfügbar ist.

## Installation

Unter [Releases](../../releases) gibt es ein DEB-Paket für Debian-basierte Linux-Systeme und portable ZIP-Pakete für Linux und Windows. Die ZIP vollständig entpacken und `Hoerwerk` beziehungsweise `Hoerwerk.exe` starten. Den lokalen Datenordner bei einem Versionswechsel sichern. Auf Linux lassen sich die Prüfsummen mit `sha256sum -c SHA256SUMS` kontrollieren.

## Entwicklung und Beiträge

Der Buildablauf mit den verwendeten Paketversionen steht in [.github/workflows/release-rc1.yml](.github/workflows/release-rc1.yml). Änderungen sind per Pull Request willkommen. Fehlerberichte bitte mit Betriebssystem, Version, reproduzierbaren Schritten und gegebenenfalls Diagnosecode einreichen. Zugangsdaten und private Bibliotheksdaten gehören ins eigene Profil.

Hörwerk steht unter der [GNU Affero General Public License v3.0](LICENSE). Für mitgelieferte Drittkomponenten gelten ihre jeweiligen Lizenztexte im Ordner `licenses` der Pakete. Audible und Amazon sind eigenständige Dienste.
