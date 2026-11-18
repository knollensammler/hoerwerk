"""Backend bauen und seine TLS-Vorbereitung im eingefrorenen Programm prüfen."""
from __future__ import annotations
import argparse,json,os,shutil,ssl,subprocess,tempfile
from pathlib import Path

class BackendBuildError(Exception):pass

def runtime_ssl_data_args():
    # Einige Python-Laufzeiten laden diese zusätzliche öffentliche Trust-Policy
    # bereits beim Anlegen eines SSLContext. Die Policy erhalten, nicht umgehen.
    policy=Path(ssl.__file__).with_name("codex-rejected-cas.pem")
    if policy.exists():
        if not policy.is_file() or policy.is_symlink():raise BackendBuildError("Unsichere SSL-Laufzeitdatei.")
        return ["--add-data",str(policy)+os.pathsep+"."]
    return []

def check_backend(binary,runner=subprocess.run):
    result=runner([str(binary),"--hoerwerk-tls-self-check"],capture_output=True,text=True,timeout=30,check=False,shell=False)
    try:value=json.loads(result.stdout)
    except (ValueError,TypeError):raise BackendBuildError("Backend liefert keinen gültigen TLS-Test.") from None
    if (result.returncode or not isinstance(value,dict) or value.get("tls_ok") is not True
            or value.get("verify_required") is not True or value.get("hostname_checked") is not True
            or type(value.get("ca_count")) is not int or value["ca_count"]<1):
        raise BackendBuildError("Der native Backend-TLS-Test ist fehlgeschlagen.")
    return value

def build_backend(source,target,pyinstaller="pyinstaller",runner=subprocess.run):
    source,target=Path(source).resolve(),Path(target).absolute()
    if target.exists() or target.is_symlink():raise BackendBuildError("Ausgabe existiert bereits.")
    if not (source/"audible_entry.py").is_file():raise BackendBuildError("Backend-Einstieg fehlt.")
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hoerwerk-backend-",dir=target.parent) as tmp:
        root=Path(tmp)
        args=[str(pyinstaller),"--onefile","--console","--name","audible","--paths",str(source),
              "--collect-all","audible_cli","--collect-all","audible","--copy-metadata","audible-cli",
              "--copy-metadata","audible","--collect-data","certifi",*runtime_ssl_data_args(),
              "--distpath",str(root/"dist"),"--workpath",str(root/"work"),"--specpath",str(root/"spec"),str(source/"audible_entry.py")]
        result=runner(args,check=False,shell=False)
        if result.returncode:raise BackendBuildError("Backend-Build fehlgeschlagen.")
        binary=root/"dist"/("audible.exe" if os.name=="nt" else "audible")
        proof=check_backend(binary,runner)
        # Erst nach bestandenem Test veröffentlichen; keine bestehende Datei überschreiben.
        with binary.open("rb") as src,target.open("xb") as dst:shutil.copyfileobj(src,dst)
        target.chmod(0o755)
    return proof

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("source",type=Path);parser.add_argument("target",type=Path)
    parser.add_argument("--pyinstaller",default="pyinstaller");args=parser.parse_args()
    print(json.dumps(build_backend(args.source,args.target,args.pyinstaller)))
