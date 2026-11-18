"""Begrenzter, abbrechbarer Controller für den externen Browser-Login."""
from __future__ import annotations
import json, os, queue, subprocess, threading, time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from core import BackendUnavailable, HoerwerkError, InvalidBackendResult, PortablePaths
from jobs import require_plain_account, stop_process
from login import readiness

from device_login import MAX_SECONDS, PUBLIC_CODE

MAX_URL = 8192
AMAZON_HOSTS = {"amazon.de", "www.amazon.de"}
class BrowserLoginError(HoerwerkError): pass
class BrowserLoginCancelled(BrowserLoginError): pass

BACKEND_ERRORS = {
    "activation": "Amazon konnte den Gerätecode nicht bereitstellen oder bestätigen. Bitte starte die Anmeldung erneut.",
    "activation_expired": "Der Amazon-Gerätecode ist abgelaufen. Bitte starte die Anmeldung erneut.",
    "existing": "Dieses Profil enthält bereits Anmeldedaten. Sie wurden nicht überschrieben.",
    "browser": "Die Browser-Anmeldung konnte nicht vorbereitet werden. Bitte neu starten.",
    "registration": "Amazon konnte die Anmeldung nicht abschließen. Bitte eine neue Anmeldung starten und nur die Rückgabe aus dem dabei geöffneten Browserfenster verwenden.",
    "invalid_code": "Amazon hat den Rückgabecode abgelehnt. Er kann abgelaufen sein oder zu einer älteren Anmeldung gehören. Bitte eine neue Anmeldung starten.",
    "tls": "Die sichere Zertifikatsprüfung für die Amazon-Verbindung ist fehlgeschlagen.",
    "dns": "Der Amazon-Servername konnte nicht aufgelöst werden. Die konkrete Ursache ist nicht bekannt.",
    "timeout": "Die Amazon-Verbindung oder ihre Antwort hat das feste Zeitlimit überschritten.",
    "proxy": "Die Verbindung über den eingestellten Proxy konnte nicht hergestellt werden.",
    "missing_file": "Eine benötigte lokale Paket- oder Zertifikatsdatei fehlt.",
    "connection": "Die Verbindung zu Amazon konnte nicht hergestellt oder gehalten werden; die konkrete Ursache ist nicht bekannt.",
    "save": "Die Anmeldung konnte nicht im Benutzerordner gespeichert werden. Bestehende Daten wurden nicht überschrieben.",
    "cancelled": "Anmeldung abgebrochen.",
}

def backend_error(message):
    return BrowserLoginError(BACKEND_ERRORS.get(message.get("reason"), "Das Anmeldeprogramm wurde unerwartet beendet. Bitte die Anmeldung neu starten."))

def _url(value, path, label):
    if not isinstance(value, str) or not value or len(value) > MAX_URL or any(ord(c) < 32 for c in value):
        raise BrowserLoginError(label)
    value = value.strip()
    try: parsed = urlsplit(value); port = parsed.port
    except ValueError: raise BrowserLoginError(label) from None
    if (parsed.scheme != "https" or parsed.hostname not in AMAZON_HOSTS or parsed.username
            or parsed.password or port not in (None, 443) or parsed.path != path or parsed.fragment):
        raise BrowserLoginError(label)
    return value, parsed

def validate_login_url(value: str) -> str:
    return _url(value, "/ap/signin", "Das Backend hat keine erlaubte Amazon-Anmeldeadresse geliefert.")[0]

def validate_callback(value: str) -> str:
    if isinstance(value, str):
        try: start = urlsplit(value.strip())
        except ValueError: start = None
        if start and start.hostname in AMAZON_HOSTS and start.path == "/ap/signin":
            raise BrowserLoginError("Das ist noch die Startadresse. Bitte erst im Browser anmelden und danach die Adresse der Amazon-Fehlerseite übernehmen.")
    value, parsed = _url(value, "/ap/maplanding", "Die Rückgabeadresse ist keine gültige Amazon-OAuth-Rückgabe.")
    values = parse_qs(parsed.query, keep_blank_values=True).get("openid.oa2.authorization_code", [])
    if len(values) != 1 or not values[0]:
        raise BrowserLoginError("Die Rückgabeadresse ist keine gültige Amazon-OAuth-Rückgabe.")
    return value

def browser_environment(environ=None):
    env = dict(os.environ if environ is None else environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is None: env.pop("LD_LIBRARY_PATH", None)
    else: env["LD_LIBRARY_PATH"] = original
    return env

class BrowserLoginController:
    def __init__(self, paths: PortablePaths, profile: str, *, factory=subprocess.Popen,
                 first_timeout=30, callback_timeout=1200, result_timeout=120, device_code=False):
        self.paths, self.profile, self.factory = paths, paths.account(profile).name, factory
        self.events, self._commands = queue.Queue(), queue.Queue()
        self._cancel, self._thread, self._process = threading.Event(), None, None
        self.timeouts = first_timeout, callback_timeout, result_timeout
        self.device_code = device_code
    @property
    def busy(self): return self._thread is not None and self._thread.is_alive()
    def start(self):
        if self.busy: raise BrowserLoginError("Eine Anmeldung läuft bereits.")
        try: readiness(self.paths, self.profile)
        except InvalidBackendResult: pass
        else: raise BrowserLoginError("Dieses Profil ist bereits lokal eingerichtet.")
        backend = self.paths.bundled_program("audible")
        if backend is None: raise BackendUnavailable("Das lokale Audible-Backend fehlt.")
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, args=(backend,), name="hoerwerk-login")
        self._thread.start()
    def submit_callback(self, value): self._commands.put(validate_callback(value))
    def cancel(self):
        self._cancel.set(); self._commands.put(None)
    def wait(self, timeout=None):
        if self._thread: self._thread.join(timeout)
        return not self.busy
    def _wait_item(self, source, timeout, error="Das Anmeldeprogramm antwortet nicht. Bitte erneut anmelden."):
        end = time.monotonic() + timeout
        while not self._cancel.is_set():
            try: return source.get(timeout=min(.1, max(0, end-time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= end: raise BrowserLoginError(error)
        raise BrowserLoginCancelled("Anmeldung abgebrochen.")
    def _run(self, backend):
        lines = queue.Queue()
        try:
            account = require_plain_account(self.paths, self.profile)
            for directory in (self.paths.data, self.paths.data/"accounts", account):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True); os.chmod(directory, 0o700)
            mode = "--hoerwerk-device-login" if self.device_code else "--hoerwerk-login"
            self._process = self.factory([str(backend), mode, str(account)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, shell=False)
            def reader():
                try:
                    while True:
                        line = self._process.stdout.readline(MAX_URL + 2)
                        lines.put(line)
                        if not line: break
                except BaseException: lines.put("")
            threading.Thread(target=reader, daemon=True).start()
            def message(timeout):
                line = self._wait_item(lines, timeout)
                if not line or len(line) > MAX_URL + 1: raise BrowserLoginError("Ungültige Backend-Antwort.")
                try: value = json.loads(line)
                except (ValueError, TypeError): raise BrowserLoginError("Ungültige Backend-Antwort.") from None
                if not isinstance(value, dict): raise BrowserLoginError("Ungültige Backend-Antwort.")
                return value
            first = message(self.timeouts[0])
            if first.get("type") == "result" and first.get("status") == "error": raise backend_error(first)
            if self.device_code:
                value = first.get("code")
                if (set(first) != {"type", "code"} or first["type"] != "device_code"
                        or not isinstance(value, str) or not PUBLIC_CODE.fullmatch(value)):
                    raise BrowserLoginError("Ungültige Antwort für die Geräteanmeldung.")
                self.events.put(("device_code", value))
                result = message(MAX_SECONDS + self.timeouts[0])
            else:
                if first.get("type") != "login_url" or not isinstance(first.get("url"), str): raise BrowserLoginError("Ungültige Backend-Antwort.")
                self.events.put(("url", validate_login_url(first["url"])))
                callback = self._wait_item(self._commands, self.timeouts[1], "Die Wartezeit für die Browser-Anmeldung ist abgelaufen. Bitte neu anmelden und die Adresse aus dem neu geöffneten Browserfenster verwenden.")
                if callback is None or self._cancel.is_set(): raise BrowserLoginCancelled("Anmeldung abgebrochen.")
                sent = queue.Queue()
                def writer():
                    try:
                        self._process.stdin.write(json.dumps({"type":"callback","url":callback}, separators=(",",":"))+"\n")
                        self._process.stdin.flush(); sent.put(True)
                    except (OSError, BrokenPipeError): sent.put(False)
                threading.Thread(target=writer, daemon=True).start()
                if not self._wait_item(sent, self.timeouts[0]):
                    raise BrowserLoginError("Die Anmeldung wurde nicht abgeschlossen.")
                result = message(self.timeouts[2])
            if result.get("type") == "result" and result.get("status") == "error": raise backend_error(result)
            if self._cancel.is_set(): raise BrowserLoginCancelled("Anmeldung abgebrochen.")
            try: code = self._process.wait(timeout=2)
            except subprocess.TimeoutExpired: raise BrowserLoginError("Die Anmeldung wurde nicht abgeschlossen.") from None
            if code or result != {"type":"result","status":"ok"}: raise BrowserLoginError("Die Anmeldung wurde nicht abgeschlossen.")
            readiness(self.paths, self.profile)
            if self._cancel.is_set(): raise BrowserLoginCancelled("Anmeldung abgebrochen.")
            self.events.put(("success", "locally_configured_unverified"))
        except BrowserLoginCancelled as exc: self.events.put(("cancelled", str(exc)))
        except BrowserLoginError as exc: self.events.put(("error", str(exc)))
        except Exception: self.events.put(("error", "Das Anmeldeprogramm wurde unerwartet beendet. Bitte die Anmeldung neu starten."))
        finally:
            if self._process is not None:
                stop_process(self._process)
                for stream in (self._process.stdin, self._process.stdout):
                    try: stream.close()
                    except (AttributeError, OSError): pass

