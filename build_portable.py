"""Sichere Vorbereitung eines nativen portablen Hörwerk-Pakets."""
from __future__ import annotations
import argparse, hashlib, json, os, platform, re, shutil, subprocess, tempfile, urllib.parse, zipfile
from pathlib import Path
from build_backend import runtime_ssl_data_args

class BuildError(Exception): pass
_HEX64=re.compile(r"[0-9a-fA-F]{64}\Z"); _HEX40=re.compile(r"[0-9a-fA-F]{40}\Z")
_FORBIDDEN_FILES={"auth.json","library.json","config.toml"}; _FORBIDDEN_DIRS={"__pycache__","accounts","profiles","cache"}
_ARCH_ALIASES={"amd64":"x86_64","x64":"x86_64","x86-64":"x86_64","aarch64":"arm64"}

def _sha(path):
    digest=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024),b""): digest.update(block)
    return digest.hexdigest()
def _normal_arch(value): return _ARCH_ALIASES.get(value.strip().lower(),value.strip().lower())
def sys_platform():
    import sys
    return sys.platform
def _host_os(): return "windows" if os.name=="nt" else "linux" if sys_platform()=="linux" else sys_platform()
def _sensitive(relative):
    lower=relative.name.lower()
    return (lower in _FORBIDDEN_FILES or lower.endswith(".voucher") or
            any(part.lower() in _FORBIDDEN_DIRS for part in relative.parts))

def _plain_file(root,value,label):
    if not isinstance(value,str) or not value or Path(value).is_absolute(): raise BuildError(f"{label} ist kein sicherer Staging-Dateipfad.")
    candidate=root/value
    try:
        relative=candidate.relative_to(root)
        if any(p in {"",".",".."} for p in relative.parts): raise ValueError
        current=root
        for part in relative.parts:
            current/=part
            if current.is_symlink(): raise ValueError
        if not candidate.is_file() or candidate.resolve()!=root/relative: raise ValueError
    except (OSError,RuntimeError,ValueError): raise BuildError(f"{label} ist kein sicherer Staging-Dateipfad.") from None
    return candidate

def _public_source(value):
    try:
        p=urllib.parse.urlsplit(value); return p.scheme in {"http","https"} and bool(p.netloc)
    except (TypeError,ValueError): return False

def load_staging(staging,target_os,target_arch):
    raw=Path(staging).absolute()
    if raw.is_symlink(): raise BuildError("Das Staging-Verzeichnis darf keine Umleitung sein.")
    staging=raw.resolve(); manifest=_plain_file(staging,"manifest.json","Manifest")
    try: data=json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError,UnicodeError,json.JSONDecodeError): raise BuildError("Das Staging-Manifest ist ungültig.") from None
    items=data.get("backends") if isinstance(data,dict) else None
    if not isinstance(items,list) or len(items)!=2: raise BuildError("Das Manifest muss audible und ffmpeg genau einmal enthalten.")
    result=[]; expected={"audible","ffmpeg"}
    for item in items:
        if not isinstance(item,dict) or not isinstance(item.get("name"),str) or item["name"] not in expected: raise BuildError("Unbekanntes oder doppeltes Backend im Staging-Manifest.")
        required=("version","source","sha256","file","license","os","arch")
        if any(not isinstance(item.get(k),str) or not item[k] for k in required): raise BuildError("Backend-Metadaten sind unvollständig oder falsch typisiert.")
        if item["version"].lower()=="unknown" or not _public_source(item["source"]): raise BuildError("Backend-Version oder öffentliche Quellen-URL ist ungültig.")
        if not _HEX64.fullmatch(item["sha256"]): raise BuildError("Backend-Hash muss aus 64 Hexzeichen bestehen.")
        if item["os"]!=target_os or _normal_arch(item["arch"])!=target_arch: raise BuildError("Backend-Zielsystem stimmt nicht mit dem nativen Build überein.")
        binary=_plain_file(staging,item["file"],"Backend"); license_file=_plain_file(staging,item["license"],"Lizenz")
        if _sha(binary).lower()!=item["sha256"].lower(): raise BuildError("Backend-Hash stimmt nicht mit dem Manifest überein.")
        if target_os!="windows" and not os.access(binary,os.X_OK): raise BuildError("Ein Backend besitzt keine Ausführungsrechte.")
        result.append({**item,"arch":target_arch,"binary_path":binary,"license_path":license_file}); expected.remove(item["name"])
    return result

def _run(runner,argv,**kwargs):
    try: result=runner(argv,shell=False,check=False,**kwargs)
    except OSError: raise BuildError("PyInstaller konnte nicht gestartet werden.") from None
    if result.returncode: raise BuildError("PyInstaller hat den Build nicht erfolgreich beendet.")
    return result
def _validate_source_tree(root):
    """Erlaubt nur interne relative Dateialias-Symlinks."""
    root=root.resolve()
    try:
        for path in root.rglob("*"):
            rel=path.relative_to(root)
            if _sensitive(rel): raise BuildError("PyInstaller-Ausgabe enthält Nutzer- oder Cachedateien.")
            if path.is_symlink():
                link=os.readlink(path)
                if Path(link).is_absolute(): raise BuildError("PyInstaller-Ausgabe enthält eine unzulässige Umleitung.")
                target=path.resolve(strict=True)
                target.relative_to(root)
                if not target.is_file() or _sensitive(target.relative_to(root)): raise BuildError("PyInstaller-Ausgabe enthält eine unzulässige Umleitung.")
    except BuildError: raise
    except (OSError,RuntimeError,ValueError): raise BuildError("PyInstaller-Ausgabe ist nicht sicher prüfbar.") from None

def _validate_plain_tree(root):
    try:
        for path in root.rglob("*"):
            rel=path.relative_to(root)
            if path.is_symlink(): raise BuildError("Das Paket enthält nach dem Kopieren eine Umleitung.")
            if _sensitive(rel): raise BuildError("PyInstaller-Ausgabe enthält Nutzer- oder Cachedateien.")
    except BuildError: raise
    except (OSError,RuntimeError): raise BuildError("PyInstaller-Ausgabe ist nicht sicher prüfbar.") from None

def _write_zip(source,target):
    with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(source.rglob("*")):
            arc=(Path(source.name)/path.relative_to(source)).as_posix()
            if path.is_dir():
                info=zipfile.ZipInfo(arc.rstrip("/")+"/"); info.external_attr=(path.stat().st_mode&0xffff)<<16; bundle.writestr(info,b"")
            else:
                info=zipfile.ZipInfo.from_file(path,arc); info.compress_type=zipfile.ZIP_DEFLATED
                with path.open("rb") as stream: bundle.writestr(info,stream.read())
def _owned_remove(path,identity):
    try:
        s=path.stat()
        if (s.st_dev,s.st_ino)==identity: shutil.rmtree(path)
    except OSError: pass
def _publish_directory(source,final):
    try: final.mkdir()
    except FileExistsError: raise BuildError("Zielverzeichnis existiert bereits.") from None
    s=final.stat(); identity=(s.st_dev,s.st_ino)
    try:
        for child in source.iterdir(): shutil.move(str(child),final/child.name)
    except BaseException: _owned_remove(final,identity); raise
    return identity
def _publish_file(source,target):
    try: os.link(source,target); return
    except FileExistsError: raise BuildError("ZIP existiert bereits.") from None
    except OSError: pass
    fd=None; identity=None
    try:
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644); st=os.fstat(fd); identity=(st.st_dev,st.st_ino)
        with source.open("rb") as src,os.fdopen(fd,"wb") as dst:
            fd=None; shutil.copyfileobj(src,dst); dst.flush(); os.fsync(dst.fileno())
    except FileExistsError: raise BuildError("ZIP existiert bereits.") from None
    except OSError:
        if fd is not None: os.close(fd)
        try:
            st=target.stat()
            if identity==(st.st_dev,st.st_ino): target.unlink()
        except OSError: pass
        raise BuildError("ZIP konnte nicht vollständig veröffentlicht werden.") from None

def _native_self_check(runner,executable,cwd):
    try:
        result=runner([str(executable),"--self-check"],shell=False,check=False,cwd=cwd,
                      stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        data=json.loads(result.stdout)
    except (OSError,TypeError,UnicodeError,json.JSONDecodeError):
        raise BuildError("Der native App-Selbsttest konnte nicht ausgewertet werden.") from None
    if (result.returncode != 0 or not isinstance(data, dict)
            or data.get("import_ok") is not True or data.get("mode") != "self-check"
            or not isinstance(data.get("platform"), str) or not data["platform"]
            or set(data) != {"import_ok", "mode", "platform"}):
        detail = (result.stderr or "").strip()[:400]
        raise BuildError("Der native App-Selbsttest ist fehlgeschlagen." + (" " + detail if detail else ""))

def build(repo_dir,output_dir,staging,*,pyinstaller=None,runner=subprocess.run,target_os=None,target_arch=None,source_sha="",host_os=None,host_arch=None):
    repo_dir,output_dir=Path(repo_dir).resolve(),Path(output_dir).resolve(); actual_os=host_os or _host_os(); actual_arch=_normal_arch(host_arch or platform.machine())
    target_os=target_os or actual_os; target_arch=_normal_arch(target_arch or actual_arch)
    if target_os!=actual_os or target_arch!=actual_arch: raise BuildError("Cross-Compilation wird nicht unterstützt; Ziel stimmt nicht mit dem Host überein.")
    if not _HEX40.fullmatch(source_sha): raise BuildError("Die Quell-SHA muss eine vollständige 40-stellige Git-SHA sein.")
    pyinstaller=pyinstaller or shutil.which("pyinstaller")
    if not pyinstaller: raise BuildError("PyInstaller ist lokal nicht vorhanden; kein Build wurde begonnen.")
    final=output_dir/f"Hoerwerk-{target_os}-{target_arch}"; archive=final.with_suffix(".zip")
    if final.exists() or archive.exists(): raise BuildError("Zielverzeichnis oder ZIP existiert bereits.")
    backends=load_staging(staging,target_os,target_arch); output_dir.mkdir(parents=True,exist_ok=True); identity=None
    try:
      with tempfile.TemporaryDirectory(prefix=".hoerwerk-build-",dir=output_dir) as tmp:
        temp=Path(tmp); dist,work,spec=temp/"dist",temp/"work",temp/"spec"
        version=_run(runner,[pyinstaller,"--version"],cwd=repo_dir,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True).stdout.strip()
        if not version or version.lower()=="unknown": raise BuildError("PyInstaller-Version ist nicht nachvollziehbar.")
        _run(runner,[pyinstaller,"--onedir","--console","--name","Hoerwerk",
                     "--hidden-import","_cffi_backend","--copy-metadata","miniaudio",
                     *runtime_ssl_data_args(),"--distpath",str(dist),"--workpath",str(work),"--specpath",str(spec),str(repo_dir/"entry.py")],cwd=repo_dir)
        built=dist/"Hoerwerk"; exe=built/("Hoerwerk.exe" if target_os=="windows" else "Hoerwerk")
        if not built.is_dir() or not exe.is_file(): raise BuildError("PyInstaller hat kein erwartetes App-Executable erzeugt.")
        if target_os!="windows" and not os.access(exe,os.X_OK): raise BuildError("Das App-Executable besitzt keine Ausführungsrechte.")
        _validate_source_tree(built); assembled=temp/final.name
        shutil.copytree(built,assembled,symlinks=False,copy_function=shutil.copy2)
        for name in ("bin","licenses","data"): (assembled/name).mkdir()
        meta=[]
        for item in backends:
            target=assembled/"bin"/(item["name"]+(".exe" if target_os=="windows" else "")); shutil.copy2(item["binary_path"],target)
            if target_os!="windows": target.chmod(target.stat().st_mode|0o111)
            shutil.copy2(item["license_path"],assembled/"licenses"/f"{item['name']}-{Path(item['license']).name}")
            meta.append({k:item[k] for k in ("name","version","source","sha256","os","arch")})
        (assembled/"build-manifest.json").write_text(json.dumps({"format":1,"native_target":{"os":target_os,"arch":target_arch},"source_sha":source_sha,"pyinstaller_version":version,"backends":meta},indent=2,sort_keys=True)+"\n",encoding="utf-8")
        _validate_plain_tree(assembled)
        hashes={p.relative_to(assembled).as_posix():_sha(p) for p in sorted(assembled.rglob("*")) if p.is_file()}
        (assembled/"files.sha256.json").write_text(json.dumps(hashes,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        _native_self_check(runner,assembled/exe.relative_to(built),assembled)
        tempzip=temp/"artifact.zip"; _write_zip(assembled,tempzip); identity=_publish_directory(assembled,final); _publish_file(tempzip,archive)
    except BaseException:
        if identity is not None: _owned_remove(final,identity)
        raise
    return archive

def main(argv=None):
    p=argparse.ArgumentParser(description="Nativen Hörwerk-Build vorbereiten"); p.add_argument("staging",type=Path); p.add_argument("output",type=Path); p.add_argument("--source-sha",required=True); a=p.parse_args(argv)
    try: artifact=build(Path(__file__).resolve().parent,a.output,a.staging,source_sha=a.source_sha)
    except BuildError as exc: p.exit(2,f"Build blockiert: {exc}\n")
    print(artifact); return 0
if __name__=="__main__": raise SystemExit(main())
