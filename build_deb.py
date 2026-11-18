"""Erzeugt ein installierbares Debian-Paket aus einem geprüften Native-Bundle."""
from __future__ import annotations
import argparse, json, os, re, shutil, stat, subprocess, tempfile
from pathlib import Path
from build_portable import BuildError, _sha, _publish_file
_VERSION=re.compile(r"[0-9][0-9A-Za-z.+~:-]*\Z"); _HEX64=re.compile(r"[0-9a-f]{64}\Z"); _SHA=re.compile(r"[0-9a-f]{40}\Z")
SENSITIVE={"auth.json","config.toml","library.json"}; LIMIT=4*1024*1024
class DebError(BuildError): pass

def _json(path):
    try:
        if not stat.S_ISREG(path.lstat().st_mode): raise ValueError
        with path.open("rb") as stream: raw=stream.read(LIMIT+1)
        if len(raw)>LIMIT: raise ValueError
        value=json.loads(raw.decode("utf-8"))
    except (OSError,UnicodeError,ValueError,RecursionError): raise DebError("Native-Manifeste sind ungültig.") from None
    return value

def _verify_bundle(root: Path, source_sha: str):
    raw=Path(root).absolute()
    if raw.is_symlink() or not raw.is_dir(): raise DebError("Native-Appverzeichnis ist ungültig.")
    for parent in (raw,*raw.parents):
        if parent.is_symlink(): raise DebError("Native-Appverzeichnis ist ungültig.")
    root=raw.resolve(); manifest=_json(root/"build-manifest.json"); hashes=_json(root/"files.sha256.json")
    if (not isinstance(manifest,dict) or manifest.get("format")!=1 or manifest.get("source_sha")!=source_sha
            or not _SHA.fullmatch(source_sha) or manifest.get("native_target")!={"os":"linux","arch":"x86_64"}
            or not isinstance(hashes,dict)):
        raise DebError("Native-Manifeste sind ungültig.")
    expected={"build-manifest.json","files.sha256.json"}; actual=set()
    for path in root.rglob("*"):
        rel=path.relative_to(root).as_posix()
        try: mode=path.lstat().st_mode
        except OSError: raise DebError("Native-Bundle ist nicht sicher lesbar.") from None
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise DebError("Native-Bundle enthält unzulässige Dateitypen.")
        if any(p.lower() in {"accounts","profiles","cache","__pycache__"} for p in path.relative_to(root).parts):
            raise DebError("Native-Bundle enthält Laufzeitdaten.")
        if path.name.lower() in SENSITIVE or path.suffix.lower()==".voucher": raise DebError("Native-Bundle enthält Laufzeitdaten.")
        if stat.S_ISREG(mode): actual.add(rel)
    if (root/"data").exists() and any((root/"data").iterdir()): raise DebError("Native-Datenverzeichnis ist nicht leer.")
    for rel,digest in hashes.items():
        if not isinstance(rel,str) or not isinstance(digest,str) or not _HEX64.fullmatch(digest): raise DebError("Native-Dateihashes sind ungültig.")
        p=Path(rel)
        if p.is_absolute() or not p.parts or any(x in {"",".",".."} for x in p.parts): raise DebError("Native-Dateihashes sind ungültig.")
        path=root/p
        if path.is_symlink() or not path.is_file() or _sha(path)!=digest: raise DebError("Native-Dateihashes sind ungültig.")
        expected.add(rel)
    if actual!=expected: raise DebError("Native-Dateimenge stimmt nicht mit dem Hashmanifest überein.")
    programs = ["Hoerwerk", "bin/audible", "bin/ffmpeg"]
    if (root / "_internal/playwright/driver").is_dir():
        programs.append("_internal/playwright/driver/node")
    for name in programs:
        path=root/name
        if not path.is_file() or not os.access(path,os.X_OK): raise DebError("Ein erforderliches Programm fehlt oder ist nicht ausführbar.")
    return root

def build_deb(native:Path,output:Path,version:str,source_sha:str,*,runner=subprocess.run)->Path:
    if not isinstance(version,str) or not _VERSION.fullmatch(version) or not isinstance(source_sha,str) or not _SHA.fullmatch(source_sha): raise DebError("Version oder Quell-SHA ist ungültig.")
    native_raw=Path(native).absolute(); output=Path(output).absolute()
    if output.exists() or output.is_symlink(): raise DebError("Ausgabedatei existiert bereits.")
    native=_verify_bundle(native_raw,source_sha); output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hoerwerk-deb-",dir=output.parent) as tmp:
        stage=Path(tmp)/"root"; app=stage/"opt/hoerwerk"; shutil.copytree(native,app,symlinks=False,copy_function=shutil.copy2)
        debian=stage/"DEBIAN"; debian.mkdir(parents=True)
        (debian/"control").write_text(f"Package: hoerwerk\nVersion: {version}\nArchitecture: amd64\nSection: sound\nPriority: optional\nMaintainer: Mika\nDepends: libc6 (>= 2.28), libx11-6, libxft2, fontconfig, xdg-utils\nDescription: Hörwerk Audible-Bibliothek und MP3-Export\n",encoding="utf-8")
        launcher=stage/"usr/bin/hoerwerk"; launcher.parent.mkdir(parents=True); launcher.write_text('#!/bin/sh\nexec /opt/hoerwerk/Hoerwerk --installed "$@"\n',encoding="utf-8")
        desktop=stage/"usr/share/applications/hoerwerk.desktop"; desktop.parent.mkdir(parents=True); desktop.write_text("[Desktop Entry]\nType=Application\nName=Hörwerk\nExec=/usr/bin/hoerwerk\nTerminal=false\nIcon=audio-headphones\nCategories=AudioVideo;Audio;\n",encoding="utf-8")
        executables = {app / "Hoerwerk", app / "bin/audible", app / "bin/ffmpeg", launcher}
        driver = app / "_internal/playwright/driver/node"
        if driver.is_file():
            executables.add(driver)
        for p in stage.rglob("*"):
            if p.is_symlink(): raise DebError("Symlinks sind im DEB-Staging nicht erlaubt.")
            p.chmod(0o755 if p.is_dir() or p in executables else 0o644)
        temp=Path(tmp)/"package.deb"
        try: result=runner(["dpkg-deb","--build","--root-owner-group","-Zxz",str(stage),str(temp)],shell=False,check=False,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        except OSError: raise DebError("dpkg-deb ist nicht verfügbar.") from None
        if result.returncode or not temp.is_file(): raise DebError("dpkg-deb konnte das Paket nicht erzeugen.")
        try: _publish_file(temp,output)
        except BuildError as exc: raise DebError(str(exc)) from None
    return output

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("native",type=Path); p.add_argument("output",type=Path); p.add_argument("--version",required=True); p.add_argument("--source-sha",required=True); a=p.parse_args(argv)
    try: print(build_deb(a.native,a.output,a.version,a.source_sha))
    except DebError as exc: p.exit(2,f"DEB blockiert: {exc}\n")
if __name__=="__main__":main()

