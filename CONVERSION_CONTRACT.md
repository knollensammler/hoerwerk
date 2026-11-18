# Hörwerk: verifizierter AAXC-/Voucher-/Konvertierungsvertrag

Stand: 2026-11-17. Dieses Dokument beschreibt nur den nächsten technischen
Vertrag; es implementiert weder Konvertierung noch Anmeldung. Es wurden keine
Konten, Titel, Schlüssel, Token oder fremden Binärdateien benutzt.

## Primärquellen und feste Bezugspunkte

* **audible-cli 0.6.0**, Tag/Commit `274539eba80a63ddccb79a9ec4f59b2fe911e232`, AGPL-3.0-only:
  [`cmd_download.py`](https://github.com/mkb79/audible-cli/blob/v0.6.0/src/audible_cli/cmds/cmd_download.py),
  [`models.py`](https://github.com/mkb79/audible-cli/blob/v0.6.0/src/audible_cli/models.py),
  [`utils.py`](https://github.com/mkb79/audible-cli/blob/v0.6.0/src/audible_cli/utils.py) und
  [`pyproject.toml`](https://github.com/mkb79/audible-cli/blob/v0.6.0/pyproject.toml).
  `models.py` fordert die Lizenz an, versucht deren Voucher mit der Audible-
  Bibliothek zu entschlüsseln und ersetzt bei Erfolg `content_license.license_response`
  im Antwortobjekt. `cmd_download.py` serialisiert genau dieses Antwortobjekt als
  gleichnamige `.voucher`-Datei und lädt die `offline_url` als AAXC (bei Format
  `Mpeg` als MP3). `utils.py` enthält hierfür keine zweite Vouchertransformation.
  Version 0.6.0 verlangt `audible>=0.11.0`, pinnt aber keine Obergrenze.
* **Audible 0.11.0**, PyPI-Provenienz-Commit
  [`9f3ccb82b5541b4ecbeda847d9e98d42992b11af`](https://github.com/mkb79/Audible/tree/9f3ccb82b5541b4ecbeda847d9e98d42992b11af),
  AGPL-3.0-only:
  [`src/audible/aescipher.py`](https://github.com/mkb79/Audible/blob/9f3ccb82b5541b4ecbeda847d9e98d42992b11af/src/audible/aescipher.py).
  `decrypt_voucher_from_licenserequest` nimmt das vollständige Lizenzobjekt und
  Authentifikatordaten; der verschlüsselte Eingabewert liegt unter
  `content_license.license_response`. Die interne Transformation liefert ein
  JSON-Objekt, dessen benötigte Felder `key` und `iv` heißen. Beide sind sensible
  titel-/geräte-/kontobezogene Werte; echte Werte gehören nie in Tests oder Logs.
* **FFmpeg-Maintainerquellen**, LGPL-2.1-or-later für die gelesenen Dateien:
  [`ffmpeg-formats.html`](https://ffmpeg.org/ffmpeg-formats.html) sowie
  [`libavformat/mov.c`](https://github.com/FFmpeg/FFmpeg/blob/64f6bcffaf2646d76463c89f13a8ae9abe0a1b66/libavformat/mov.c).
  Die Formatedokumentation nennt `.aa` getrennt und beschreibt bei AAX nur den
  4-Byte-`activation_bytes`-Weg. Der MOV-Demuxer implementiert darüber hinaus
  AAXC: die Eingabeoptionen heißen `audible_key` und `audible_iv`, jeweils exakt
  16 Byte; damit wird der AAXC-Bytestrom beim Demuxen entschlüsselt. AAX und AAXC
  sind daher ausdrücklich nicht derselbe Vertrag. Quellbezug ist
  Commit `64f6bcffaf2646d76463c89f13a8ae9abe0a1b66`; das auszuliefernde
  Binärpaket muss zusätzlich auf einen geprüften Release/Build gepinnt werden.
  Die Hexdarstellung ist zusätzlich in `libavutil/opt.c`, Funktion
  `set_string_binary`, derselben Commitfassung gegengeprüft.

## Verifizierte Datenkette

1. Hörwerk startet `audible download --aaxc --asin <ASIN> --output-dir <DIR>
   --no-confirm --no-progress --jobs 1 --filename-mode asin_only` im isolierten
   Profil. audible-cli prüft Berechtigung und fordert eine Lizenz an.
2. Das Backend erhält zunächst ein Lizenzobjekt. Vor erfolgreicher lokaler
   Transformation ist `content_license.license_response` ein verschlüsselter,
   base64-kodierter String; `content_license.status_code` muss `Granted` sein.
3. `models.py` ruft die Audible-Bibliotheksfunktion mit dem vollständigen Objekt
   und den bereits lokal vorhandenen Authentifikatordaten auf. Nur bei Erfolg
   wird `license_response` durch ein Objekt ersetzt, das `key` und `iv` enthält.
   **Folgerung aus dem gelesenen Code:** Die von audible-cli 0.6.0 danach
   geschriebene `.voucher`-Datei enthält normalerweise bereits die entschlüsselte
   Objektform. Schlägt die Transformation fehl, protokolliert das Backend den
   Fehler, schreibt aber weiterhin das Objekt; Hörwerk darf die Stringform daher
   keinesfalls als entschlüsselt behandeln.
4. Die gleichnamige Audiodatei ist entweder bereits MP3 (`Mpeg`) oder AAXC. Für
   AAXC müssen Voucherstatus, Objektform sowie je ein syntaktisch gültiger
   16-Byte-Wert aus `key`/`iv` geprüft werden. Kodierung und Übergabeformat
   (FFmpeg-`AV_OPT_TYPE_BINARY` erwartet eine Hexdarstellung) sind vor Nutzung
   streng zu validieren; nie Werte erraten oder normalisieren.
5. FFmpeg kann AAXC direkt demuxen, wenn `-audible_key <hex>` und
   `-audible_iv <hex>` **vor** `-i <datei.aaxc>` stehen. Danach kann der bestehende
   MP3-Ausgabevertrag (`-map 0:a:0 -vn -codec:a libmp3lame ...`) greifen.

## Lokal möglich, nicht belegt oder bewusst offen

Mit den vorhandenen Bausteinen ist die Kette technisch lokal möglich: Backend
lädt AAXC/Voucher, Audible entschlüsselt den Voucher im berechtigten Profil,
FFmpeg entschlüsselt/demuxiert und transkodiert. Vorhandener Hörwerk-Code lehnt
AAXC jedoch korrekt ab; er implementiert weder Schemaprüfung noch geheime
Argumentübergabe. Nicht geprüft wurden echte Voucher, ein realer FFmpeg-Lauf,
Windows/USB, Abbruch unter Last und ein Ende-zu-Ende-Konto. Ebenfalls offen sind
die exakt zu bündelnde FFmpeg-Version/Buildoptionen und die Lizenzgestaltung des
GsG-Originalcodes. Aus den Fremdlizenzen wird hier keine Rechtsentscheidung
abgeleitet. Libation/AaxAudioConverter ist für den kleinsten Schritt nicht nötig;
es wurde deshalb kein Fremdcode daraus übernommen oder als eingebettete API
behauptet.

## Kleinster nächster Implementierungsschritt

Eine neue, kleine AAXC-Adapterfunktion soll ausschließlich die zu einer AAXC
gleichnamige Voucherdatei öffnen, UTF-8/JSON, `Granted`, Objektform und exakt
32 Hexzeichen pro `key`/`iv` validieren und FFmpeg per `argv`/`shell=False`
starten. Ziel bleibt eine temporäre Datei im Zielordner mit der vorhandenen
exklusiven Veröffentlichung. Bestehende Dateien und Quelldateien bleiben
unangetastet. Keine Bibliotheksquelle wird importiert und keine Anmeldung ergänzt.

Schlüssel/IV dürfen weder in Exceptiontext, stdout/stderr, Prozessprotokoll noch
Diagnosedatei erscheinen. Die unvermeidbare Grenze ist, dass argv auf üblichen
Systemen für andere lokale Prozesse desselben Nutzers sichtbar sein kann. Deshalb
muss die UI vorab darauf hinweisen, Logs redigieren, den Prozess nur kurz leben
lassen und temporäre Python-Strings/Objekte sofort verwerfen; vollständiges
Löschen aus Interpreter- und Betriebssystemspeicher kann nicht garantiert werden.
Wenn eine spätere FFmpeg-Schnittstelle sichere Optionsübergabe ohne argv belegt
wird, ist sie vorzuziehen. Abbruch beendet FFmpeg, wartet darauf und entfernt nur
die eigene Teilausgabe; Voucher/AAXC und fremde Ziele bleiben bestehen.

## Synthetischer Testplan und tatsächlich ausgeführte Prüfung

* Fake-Voucher: `Granted` plus 32-hexstellige Platzhalter; prüfen, dass korrekte
  Optionen vor `-i` stehen, aber Runner-/Fehlerdarstellung sie nie ausgibt.
* Ablehnen: String-`license_response`, fehlende/falsche Typen, nicht `Granted`,
  Nicht-Hex, falsche Länge, Namensabweichung/Traversal/Symlinkausbruch, leere AAXC.
* Fake-Runner: Exitfehler, Abbruch, fehlende/leere Ausgabe, vorhandenes und
  konkurrierendes Ziel; Teilausgabe weg, Quelle/Voucher/fremder Inhalt unverändert.
* Gelesen wurden die oben verlinkten Maintainerdateien und das Schema entlang der
  Aufrufkette; zusätzlich wurden die FFmpeg-Dokumentation und MOV-Optionen
  gegengeprüft. Es gab **keine** Anmeldung, Entschlüsselung, Konvertierung,
  Binärinstallation, echten Schlüssel-, Titel- oder Kontotest.

Mika hat die angegebenen Kernfundstellen am 17.11.2026 unabhängig zurückgelesen:
audible-cli-Tag/Model/Download/Abhängigkeit, Audible-Transformation und -Version,
FFmpeg-MOV-Optionen und Hexparser. Dies ist Quellenprüfung, kein Medientest.
