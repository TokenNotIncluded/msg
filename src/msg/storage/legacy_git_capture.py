"""Read-only SSH fixed-ref bundle capture. Never writes files on the source host."""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from msg.core.errors import require
from msg.storage.legacy_git_import import sha256_file, verify_bundle
from msg.storage.legacy_rehearsal import _write

REMOTE_PROGRAM = r"""import os,sys,subprocess,json,re
path=SOURCE_PATH
env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','LANG':'C.UTF-8','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_OPTIONAL_LOCKS':'0','GIT_NO_REPLACE_OBJECTS':'1','GIT_TERMINAL_PROMPT':'0'}
command=['git','--git-dir',path,'-c','safe.directory='+path,'-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false','-c','pack.threads=1']
def run(*args):
 p=subprocess.run(command+list(args),capture_output=True,env=env,timeout=120)
 if p.returncode:raise RuntimeError('git read failed')
 return p.stdout
def refs():
 result={}
 for line in run('for-each-ref','--format=%(objectname) %(refname)').decode().splitlines():
  oid,name=line.split(' ',1)
  if not re.fullmatch(r'[0-9a-f]{40}',oid) or not re.fullmatch(r'refs/(heads|tags)/[^\s]+',name):raise RuntimeError('unsupported refs')
  result[name]=oid
 if not 0<len(result)<=128:raise RuntimeError('empty or excessive refs')
 return result
try:
 if run('rev-parse','--is-bare-repository').strip()!=b'true':raise RuntimeError('not bare')
 before=refs()
 head=run('symbolic-ref','HEAD').decode().strip()
 if head not in before:raise RuntimeError('unborn or missing default ref')
 bundle=subprocess.run(command+['bundle','create','-',*sorted(before)],stdout=sys.stdout.buffer,stderr=subprocess.PIPE,env=env,timeout=120)
 if bundle.returncode:raise RuntimeError('bundle failed')
 if refs()!=before or run('symbolic-ref','HEAD').decode().strip()!=head:raise RuntimeError('concurrent refs changed')
 sys.stderr.write(json.dumps({'format':'msg-legacy-git-source-v1','refs':before,'default_ref':head,'refs_stable':True,'scope':'reachable-heads-and-tags-only'})+'\n')
except BaseException:
 sys.stderr.write('capture_failed\n')
 sys.exit(1)
"""


def capture(host, source, protected_target):
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host) is not None, "invalid_ssh_host")
    target = Path(protected_target)
    require(
        target.is_dir() and not target.is_symlink() and target.stat().st_mode & 0o077 == 0,
        "private_target_required",
    )
    destination = target / "repository.bundle"
    require(not (target / "manifest.json").exists(), "capture_manifest_exists")
    script = REMOTE_PROGRAM.replace("SOURCE_PATH", repr(str(source)))
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            completed = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", host, "sudo", "-n", "python3", "-"],
                input=script.encode(),
                stdout=stream,
                stderr=subprocess.PIPE,
                timeout=150,
            )
            stream.flush()
            os.fsync(stream.fileno())
        require(completed.returncode == 0, "legacy_git_capture_failed")
        manifest = json.loads(completed.stderr)
        checked = verify_bundle(
            destination, sha256_file(destination), protected_work=target / "verification"
        )
        require(
            manifest["refs_stable"] is True and checked["refs"] == manifest["refs"],
            "legacy_git_source_changed",
        )
        manifest.update({key: value for key, value in checked.items() if key != "refs"})
        _write(target / "manifest.json", manifest)
        return {key: value for key, value in manifest.items() if key not in {"refs", "default_ref"}}
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--private-source-descriptor", type=Path, required=True)
    parser.add_argument("--protected-target", type=Path, required=True)
    args = parser.parse_args()
    try:
        require(
            not args.private_source_descriptor.is_symlink()
            and args.private_source_descriptor.stat().st_mode & 0o077 == 0,
            "private_source_descriptor_required",
        )
        paths = json.loads(args.private_source_descriptor.read_text())["bare_repo_paths"]
        require(len(paths) == 1, "source_repository_count_invalid")
        print(json.dumps(capture(args.ssh_host, paths[0], args.protected_target), sort_keys=True))
    except BaseException:
        # Never emit Git stderr/ref names or raw database/content exceptions.
        print(json.dumps({"result": "failed", "scope": "fixed-ref-bundle"}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
