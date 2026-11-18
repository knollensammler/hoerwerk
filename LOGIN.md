# Hörwerk: lokale interaktive Anmeldung

Dieses Paket stellt noch keine GUI und keinen nachgewiesenen Kontozugang bereit.
Es startet ausschließlich lokal `audible quickstart` in einem echten Terminal.
Jedes Hörwerk-Profil erhält ein eigenes Verzeichnis unter
`data/accounts/<profil>`. Eine vorhandene Konfiguration wird nicht gelöscht oder
überschrieben; `locally_configured_unverified` bedeutet nur, dass plausible
lokale Dateien vorhanden sind, nicht dass Anmeldung oder Token funktionieren.
Bei bestehender Konfiguration prueft der Wrapper nur diese lokalen Metadaten und
startet quickstart nicht erneut. Ungueltige Bestandsdateien bleiben unveraendert.

## Späterer lokaler Test durch die Kontoinhaberin oder den Kontoinhaber

Vom Repository-Stamm, mit absolutem Pfad zum lokal bereitgestellten Backend:

```console
python3 apps/hoerwerk/login.py /absoluter/pfad/zur/app meinprofil --backend /absoluter/pfad/zu/audible
```

Die Fragen stammen unverändert aus `audible-cli`. Bei **“Do you want to login
with external browser?” ausdrücklich “Yes” wählen**. Amazon-Benutzername und
-Kennwort ausschließlich in dem lokal geöffneten Browser eingeben, niemals in
Chat, Tickets, Git, Hörwerk-Logs oder Testdateien. Hörwerk erfasst, vervollständigt
oder protokolliert weder Kennwort noch Authdatei oder Callback-URL.

Der Readiness-Check ist offline und liest nur `config.toml` sowie Dateimetadaten.
Der Inhalt der Authdatei wird nie gelesen. Bestehende Kontodateien werden auch bei
Fehler oder Abbruch nicht aufgeräumt. Deshalb muss die Person unvollständige lokale
Einrichtungen bewusst prüfen, statt sie als erfolgreiche Anmeldung anzusehen.

## Grenzen und nächste Schritte

* Kein Cloudlauf führt eine Anmeldung, Kontoänderung oder Netzwerkprüfung aus.
* Ein verschlüsseltes Authfile kann beim späteren Bibliothekslauf eine weitere
  lokale Interaktion erfordern; das ist noch nicht gelöst.
* Bibliotheksabgleich, deutsche GUI und echte Kontoabnahme folgen später.
* AAXC-Entschlüsselung und die vollständige MP3-Kette sind nicht Teil dieses Pakets.
* Portable Linux-/Windows-Pakete und physische Tests stehen noch aus.

Für eine Distribution bleiben die Lizenzen der Bausteine zu beachten, insbesondere
`audible-cli` 0.6.0 (AGPL-3.0-only). Es werden keine Backends automatisch geladen.
