# Hörwerk: lokaler AAXC-Adapter

## Stand

Der Adapter akzeptiert nur eine nichtleere `.aaxc` im symlinkfreien Pfad
`data/accounts/<profil>/downloads/<ASIN>/` des ausgewählten Profils. Lexikalische
`..`-Traversal, Querprofile und Umleitungen in Profil-, Download-, ASIN-, Audio-
oder Voucherpfad werden abgewiesen. Der gleichnamige Voucher wird mit einem
Lesebudget von höchstens 4 MiB plus einem Prüfbyte gelesen; unbegrenztes Einlesen
findet nicht statt. Er muss UTF-8-JSON, eine gewährte Lizenz mit identischer ASIN
und je genau 32 Hexzeichen in `key` und `iv` enthalten.

FFmpeg wird lokal per Argumentliste und `shell=False` gestartet. Schlüssel- und
IV-Option stehen vor `-i`; Standard-Ein-/Ausgaben werden verworfen. Erfolg setzt
Exitcode null, eine nichtleere eigene Temporärdatei und die bestehende exklusive
No-Clobber-Veröffentlichung voraus. Ein Abbruch wird auch unmittelbar vor der
Veröffentlichung erneut geprüft. Terminate-Rennen führen das abschließende Wait
weiter aus; nach Timeout folgen Kill und Wait. Nur die eigene Temporärdatei wird
entfernt, Quellen, Voucher und fremde Ziele bleiben unverändert.

## Geheimnisgrenze

Schlüssel und IV erscheinen weder in Meldungen noch in Prozessausgaben oder
Diagnosedateien. Die Übergabe über `argv` bleibt für andere lokale Prozesse
desselben Nutzers potenziell sichtbar. Temporäre Python-Referenzen werden früh
verworfen; vollständiges Löschen aus Interpreter- oder Betriebssystemspeicher
kann nicht garantiert werden. Vor späterer Nutzung muss die Oberfläche darauf
hinweisen. Eine belegte sicherere FFmpeg-Optionsübergabe wäre vorzuziehen.

## Synthetische Tests

Vom Repository-Stamm:

    python3 -m unittest discover -s apps/hoerwerk -p 'test_*.py' -v

Die Tests verwenden nur Platzhalter, Fake-Prozesse und temporäre Dateien. Sie
prüfen echte Symlinks und Querprofile, Traversal, Leselimit, Voucher-Schema,
Optionsreihenfolge, redigierte Fehler, späten Abbruch, Terminate-/Poll-Rennen,
Timeout mit Kill/Wait, KeyboardInterrupt, No-Clobber und USB-Kopierfallback.
Sie entschlüsseln oder konvertieren keine echte Datei.

## Offene Grenzen

Dies ist keine bestätigte reale Audible-Konvertierung und keine fertige App.
Offen bleiben der auf AAXC, MPEG/`libmp3lame` und Lizenzen geprüfte konkrete
FFmpeg-Build, portable Linux-/Windows-Pakete, physische USB-Tests, Anmeldung und
Bibliothekslauf mit eigenem Konto sowie ein zulässiger Ende-zu-Ende-Test mit
gekauftem Titel. Gebündelte Komponenten folgen erst in einem späteren Paket.
