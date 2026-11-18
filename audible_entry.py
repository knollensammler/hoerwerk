"""Audible-CLI-Einstieg mit begrenztem Hörwerk-Loginprotokoll."""
from __future__ import annotations
import contextlib, errno, json, os, re, signal, socket, ssl, stat, sys, tempfile, threading, time
from pathlib import Path
from browser_login import validate_callback, validate_login_url
from device_login import ActivationExpired, activate
from transfer_progress import DOWNLOAD_OPTION, DOWNLOAD_PROGRESS

MAX_LINE=8192
DOWNLOAD_MARKER="HOERWERK-CLI-V1 "
DOWNLOAD_REASONS={"tls","dns","timeout","proxy","missing_component","local_file","license_denied","title_unavailable","license_denied_throttled","license_denied_client","license_denied_ownership","license_denied_membership","access_denied","resource_missing","throttled","unknown"}
DOWNLOAD_DIAGNOSTICS={f"D-{phase}-{kind}" for phase in "CLAMFU" for kind in "KVRABNOX"}

def _causes(exc):
    seen=set()
    for _ in range(8):
        if exc is None or id(exc) in seen: break
        seen.add(id(exc)); yield exc
        exc=exc.__cause__ if exc.__cause__ is not None else exc.__context__

def error_reason(exc, stage):
    if isinstance(exc, (KeyboardInterrupt, SystemExit)): return "cancelled"
    if isinstance(exc, ActivationExpired): return "activation_expired"
    if stage == "save" and isinstance(exc, OSError): return "save"
    if stage == "registration" and exc.args and isinstance(exc.args[0], dict):
        value=exc.args[0].get("response", {})
        value=value.get("error", {}) if isinstance(value,dict) else {}
        code=value.get("code") if isinstance(value,dict) else None
        if isinstance(code,str) and code in {"InvalidCodePair", "invalid_grant", "InvalidGrant", "InvalidToken"}: return "invalid_code"
    chain=list(_causes(exc)); names={type(item).__name__ for item in chain}
    if any(isinstance(item,(ssl.SSLError,ssl.CertificateError)) for item in chain) or names & {"CertificateError"}:
        return "tls"
    if any(isinstance(item,socket.gaierror) for item in chain): return "dns"
    if any(isinstance(item,(TimeoutError,socket.timeout)) for item in chain) or names & {"ConnectTimeout","ReadTimeout","PoolTimeout","WriteTimeout","NotResponding"}:
        return "timeout"
    if names & {"ProxyError"}: return "proxy"
    if any(isinstance(item,(FileNotFoundError,ModuleNotFoundError)) for item in chain): return "missing_file"
    connection_errnos={errno.ECONNABORTED,errno.ECONNREFUSED,errno.ECONNRESET,errno.ENETDOWN,errno.ENETUNREACH,errno.EHOSTUNREACH}
    if names & {"ConnectError","NetworkError"} or any(isinstance(item,OSError) and item.errno in connection_errnos for item in chain):
        return "connection"
    return stage

class LicenseResponseDenied(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _license_denial_reason(response):
    if not isinstance(response, dict):
        return None
    license_data = response.get("content_license")
    if not isinstance(license_data, dict) or license_data.get("status_code") != "Denied":
        return None
    reasons = license_data.get("license_denial_reasons")
    if not isinstance(reasons, list):
        return "license_denied"

    categories = set()
    for detail in reasons[:32]:
        if not isinstance(detail, dict):
            continue
        rejection = detail.get("rejectionReason")
        kind = detail.get("validationType")
        if rejection == "CustomerThrottled":
            return "license_denied_throttled"
        if rejection != "RequesterEligibility":
            continue
        for known_kind, category in (
            ("Client", "license_denied_client"),
            ("Ownership", "license_denied_ownership"),
            ("Membership", "license_denied_membership"),
        ):
            if kind == known_kind:
                categories.add(category)
    if len(categories) == 1:
        return categories.pop()
    return "license_denied"


@contextlib.contextmanager
def _download_license_reasons():
    """Preserve safe denial categories in the dedicated download subprocess."""
    from audible import AsyncClient

    original = AsyncClient.post

    async def post(client, path, *args, **kwargs):
        response = await original(client, path, *args, **kwargs)
        if isinstance(path, str) and path.startswith("content/") and path.endswith("/licenserequest"):
            reason = _license_denial_reason(response)
            if reason is not None:
                raise LicenseResponseDenied(reason)
        return response

    AsyncClient.post = post
    try:
        yield
    finally:
        AsyncClient.post = original


def download_error_reason(exc):
    """Ordnet nur belegte Exceptiontypen festen Downloadkategorien zu."""
    chain=list(_causes(exc)); names={type(item).__name__ for item in chain}
    if any(isinstance(item,(ssl.SSLError,ssl.CertificateError)) for item in chain) or names & {"CertificateError"}: return "tls"
    if any(isinstance(item,socket.gaierror) for item in chain): return "dns"
    if any(isinstance(item,(TimeoutError,socket.timeout)) for item in chain) or names & {"ConnectTimeout","ReadTimeout","PoolTimeout","WriteTimeout","NotResponding"}: return "timeout"
    if names & {"ProxyError"}: return "proxy"
    if any(isinstance(item,(ModuleNotFoundError,ImportError)) for item in chain): return "missing_component"
    statuses=[getattr(item,"status_code",None) for item in chain]
    statuses.extend(getattr(getattr(item,"response",None),"status_code",None) for item in chain)
    status=next((value for value in statuses if type(value) is int),None)
    for item in chain:
        if isinstance(item, LicenseResponseDenied):
            return item.reason
    if names & {"LicenseDenied"}: return "license_denied"
    if status in {401,403}: return "access_denied"
    if status in {404,410}: return "resource_missing"
    if status == 429:return "throttled"
    if names & {"NoDownloadUrl","ItemNotPublished","NotDownloadableAsAAX"}: return "title_unavailable"
    if names & {"FileDoesNotExists","DirectoryDoesNotExists"}:return "local_file"
    local_errnos={errno.EACCES,errno.ENOSPC,errno.EROFS,errno.EDQUOT,errno.ENAMETOOLONG}
    if any(isinstance(item,OSError) and (isinstance(item,(FileNotFoundError,PermissionError)) or item.errno in local_errnos) for item in chain): return "local_file"
    return "unknown"

def download_diagnostic(exc):
    """Nur endliche Programmkennungen; keine Werte aus Texten oder Frames."""
    chain=list(_causes(exc)); phase="U"
    for item in reversed(chain):
        tb=item.__traceback__
        for _ in range(32):
            if tb is None:break
            module=tb.tb_frame.f_globals.get("__name__")
            name=tb.tb_frame.f_code.co_name
            if module=="audible_cli.config":phase="C"
            elif module=="audible_cli.models" and name in {"from_api","from_api_full_sync","get_child_items"}:phase="L"
            elif module=="audible_cli.models" and name in {"get_license","get_aaxc_url"}:phase="A"
            elif module=="audible_cli.downloader":phase="M"
            elif module=="aiofiles.threadpool" and name in {"_open","open"}:phase="F"
            tb=tb.tb_next
        # Die ursprüngliche innere Ursache hat Vorrang vor ihrem Sammelfehler.
        if phase!="U":break
    original=chain[-1] if chain else exc
    name=type(original).__name__
    if isinstance(original,(ssl.SSLError,socket.gaierror,TimeoutError)) or name in {"ConnectError","ConnectTimeout","ReadTimeout","PoolTimeout","WriteTimeout","ProxyError","NetworkError"}:kind="N"
    elif isinstance(original,KeyError):kind="K"
    elif isinstance(original,ValueError):kind="V"
    elif isinstance(original,RuntimeError):kind="R"
    elif name=="Abort":kind="A"
    elif name in {"UsageError","BadParameter","BadOptionUsage","NoSuchOption","MissingParameter"}:kind="B"
    elif isinstance(original,OSError):kind="O"
    else:kind="X"
    return f"D-{phase}-{kind}"

def _send_download(status, reason=None, diagnostic="D-U-X"):
    value={"status":status}
    if status=="error":
        value["reason"]=reason if reason in DOWNLOAD_REASONS else "unknown"
        value["diagnostic"]=diagnostic if diagnostic in DOWNLOAD_DIAGNOSTICS else "D-U-X"
    sys.stdout.write(DOWNLOAD_MARKER+json.dumps(value,separators=(",",":"))+"\n");sys.stdout.flush()

@contextlib.contextmanager
def _download_worker_causes():
    """Erhält den ersten Workerfehler im einzelnen gepinnten CLI-Unterprozess."""
    from audible_cli.cmds import cmd_download
    original=cmd_download.DownloadRun
    class DownloadRunWithCause(original):
        def raise_for_errors(self):
            try:
                return super().raise_for_errors()
            except Exception as summary:
                # Der echte CLI-Worker speichert Originalexceptions in errors.
                # Keine Texte auswerten oder ausgeben; die vorhandene Whitelist
                # darf die begrenzte Ursache typisieren.
                if self.errors:
                    raise summary from self.errors[0]
                raise
    cmd_download.DownloadRun=DownloadRunWithCause
    try:
        yield
    finally:
        cmd_download.DownloadRun=original

@contextlib.contextmanager
def _download_progress(protocol):
    """Emit only byte counts and validated ASINs from the pinned downloader."""
    from audible_cli import downloader
    original = downloader.get_progressbar

    def progressbar(destination, total, start=0):
        match = re.match(r"^([A-Z0-9]{10})-", destination.name)
        if not match:
            return original(destination, total, start)
        asin = match[1]

        class Meter(downloader.DummyProgressBar):
            received = start
            last = 0.0

            def update(self, count=0):
                self.received += count
                now = time.monotonic()
                if not count or now - self.last >= 0.2 or self.received == total:
                    self.last = now
                    protocol.write(f"{DOWNLOAD_PROGRESS}{asin} {self.received} {total or 0}\n")
                    protocol.flush()

        meter = Meter()
        meter.update()
        return meter

    downloader.get_progressbar = progressbar
    try:
        yield
    finally:
        downloader.get_progressbar = original


def hoerwerk_download(args) -> int:
    """Kapselt ausschließlich den bestehenden Download-CLI-Aufruf."""
    if not args or args[0] != "download":
        _send_download("error","unknown");return 2
    protocol=sys.stdout
    progress = DOWNLOAD_OPTION in args
    args = [arg for arg in args if arg != DOWNLOAD_OPTION]
    try:
        from audible_cli.cli import cli
        with open(os.devnull,"w",encoding="utf-8") as private, \
             contextlib.redirect_stdout(private),contextlib.redirect_stderr(private), \
             _download_worker_causes(), _download_license_reasons(), \
             (_download_progress(protocol) if progress else contextlib.nullcontext()):
            cli(args=list(args),standalone_mode=False)
    except BaseException as exc:
        sys.stdout=protocol
        _send_download("error",download_error_reason(exc),download_diagnostic(exc));return 2
    sys.stdout=protocol
    _send_download("ok");return 0

def _send(value):
    sys.stdout.write(json.dumps(value,separators=(",",":"))+"\n"); sys.stdout.flush()
def _plain_account(value):
    account=Path(value).absolute()
    if account.is_symlink(): raise ValueError
    parent=account.parent
    if parent.is_symlink(): raise ValueError
    if any(p.is_symlink() for p in account.parents) or account.resolve() != account:
        raise ValueError
    if account.exists() and not account.is_dir(): raise ValueError
    account.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(account,0o700)
    if account.resolve()!=account: raise ValueError
    return account
def _owned_unlink(path, identity):
    try:
        info=path.stat(follow_symlinks=False)
        if identity==(info.st_dev,info.st_ino): path.unlink()
    except OSError: pass
def _publish(source,target):
    if target.exists() or target.is_symlink(): raise FileExistsError
    info=source.stat(follow_symlinks=False)
    os.link(source,target)
    return info.st_dev,info.st_ino

def hoerwerk_login(account: Path, authenticator=None, *, device_code=False) -> int:
    old_umask=os.umask(0o077); work=None; auth_identity=config_identity=None; stage="save"
    previous_signal = None
    if threading.current_thread() is threading.main_thread():
        previous_signal = signal.getsignal(signal.SIGTERM)
        def stop_login(*_): raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop_login)
    try:
        account=_plain_account(account); auth,config=account/"auth.json",account/"config.toml"
        if auth.exists() or auth.is_symlink() or config.exists() or config.is_symlink():
            _send({"type":"result","status":"error","reason":"existing"}); return 2
        if authenticator is None:
            from audible import Authenticator

            authenticator = Authenticator
        def callback(url):
            nonlocal stage
            _send({"type":"login_url","url":validate_login_url(url)})
            line=sys.stdin.readline(MAX_LINE+2)
            if not line or len(line)>MAX_LINE+1: raise ValueError
            msg=json.loads(line)
            if not isinstance(msg,dict) or msg.get("type")!="callback" or not isinstance(msg.get("url"),str): raise ValueError
            result=validate_callback(msg["url"]); stage="registration"; return result
        if device_code:
            stage = "activation"
            obj = activate(lambda code: _send({"type": "device_code", "code": code}), authenticator)
        else:
            stage = "browser"
            obj = authenticator.from_login_external(
                locale="de", with_username=False, login_url_callback=callback,
            )
        stage="save"
        work=Path(tempfile.mkdtemp(prefix=".login-",dir=account)); os.chmod(work,0o700)
        temp_auth=work/"auth"; fd=os.open(temp_auth,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.close(fd)
        obj.to_file(filename=temp_auth,encryption=False); os.chmod(temp_auth,0o600)
        temp_config=work/"config"; fd=os.open(temp_config,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as stream:
            stream.write('[APP]\nprimary_profile="audible"\n[profile.audible]\nauth_file="auth.json"\ncountry_code="de"\n')
        if auth.exists() or auth.is_symlink() or config.exists() or config.is_symlink(): raise FileExistsError
        auth_identity=_publish(temp_auth,auth)
        try: config_identity=_publish(temp_config,config)
        except BaseException: _owned_unlink(auth,auth_identity); raise
        _send({"type":"result","status":"ok"}); return 0
    except BaseException as exc:
        if config_identity is not None: _owned_unlink(config,config_identity)
        if auth_identity is not None: _owned_unlink(auth,auth_identity)
        try: _send({"type":"result","status":"error","reason":error_reason(exc,stage)})
        except BaseException: pass
        return 2
    finally:
        if work:
            import shutil; shutil.rmtree(work,ignore_errors=True)
        os.umask(old_umask)
        if previous_signal is not None: signal.signal(signal.SIGTERM,previous_signal)

def hoerwerk_library(output: Path) -> None:
    """Keep the upstream export and attach only its offline-permission verdict."""
    from audible_cli.cli import cli
    from audible_cli.cmds import cmd_library

    base_library = cmd_library.Library
    rights_by_asin = {}

    class LibraryWithOfflineRights(base_library):
        @classmethod
        async def from_api_full_sync(cls, client, **kwargs):
            groups = kwargs["response_groups"]
            kwargs["response_groups"] = groups + ", customer_rights"
            library = await super().from_api_full_sync(client, **kwargs)
            for item in library:
                rights = item.customer_rights
                offline = rights.get("is_consumable_offline") if isinstance(rights, dict) else None
                allowed = offline if type(offline) is bool else None
                if allowed is True:
                    try:
                        allowed = item.is_published()
                    except (TypeError, ValueError):
                        allowed = None
                if item.asin in rights_by_asin and rights_by_asin[item.asin] is not allowed:
                    allowed = None
                rights_by_asin[item.asin] = allowed
            return library

    cmd_library.Library = LibraryWithOfflineRights
    try:
        cli(args=["library", "export", "--format", "json", "--output", str(output)],
            standalone_mode=False)
    finally:
        cmd_library.Library = base_library
    with output.open(encoding="utf-8") as stream:
        items = json.load(stream)
    for item in items:
        item["download_allowed"] = rights_by_asin.get(item["asin"])
    with output.open("w", encoding="utf-8") as stream:
        json.dump(items, stream)

def main():
    if len(sys.argv) == 4 and sys.argv[1:3] == ["--hoerwerk-library", "--output"]:
        hoerwerk_library(Path(sys.argv[3]))
        return
    if sys.argv[1:]==["--hoerwerk-tls-self-check"]:
        try:
            import certifi
            context=ssl.create_default_context(cafile=certifi.where())
            value={"tls_ok":True,"ca_count":len(context.get_ca_certs()),
                   "verify_required":context.verify_mode==ssl.CERT_REQUIRED,
                   "hostname_checked":context.check_hostname}
        except Exception as exc:
            _send({"tls_ok":False,"reason":error_reason(exc,"browser")});raise SystemExit(2)
        _send(value);raise SystemExit(0)
    if len(sys.argv) == 3 and sys.argv[1] in {"--hoerwerk-login", "--hoerwerk-device-login"}:
        raise SystemExit(hoerwerk_login(Path(sys.argv[2]), device_code=sys.argv[1] == "--hoerwerk-device-login"))
    if len(sys.argv)>=3 and sys.argv[1]=="--hoerwerk-download": raise SystemExit(hoerwerk_download(sys.argv[2:]))
    from audible_cli import main as cli_main
    cli_main()
if __name__=="__main__": main()

