"""Check that the build contains the release-owned rules and installable entry points."""

import sys
import tomllib
from pathlib import Path
from tarfile import open as open_tar
from zipfile import ZipFile

root = Path(__file__).resolve().parents[1]
dist = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "dist"
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
wheels = sorted(dist.glob(f"msg_lmm_best-{version}-*.whl"))
sdists = sorted(dist.glob(f"msg_lmm_best-{version}.tar.gz"))
if len(wheels) != 1 or len(sdists) != 1:
    raise SystemExit("expected exactly one wheel and one source distribution")

with ZipFile(wheels[0]) as wheel:
    names = set(wheel.namelist())
    for source in (root / "src/msg/data").rglob("*"):
        if not source.is_file() or "__pycache__" in source.parts or source.suffix in {".pyc", ".pyo"}:
            continue
        target = "msg/data/" + source.relative_to(root / "src/msg/data").as_posix()
        if target not in names or wheel.read(target) != source.read_bytes():
            raise SystemExit(f"missing or changed wheel resource: {target}")
    entry_files = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
    if len(entry_files) != 1:
        raise SystemExit("missing wheel entry points")
    entries = wheel.read(entry_files[0]).decode()
    if "msg = msg.cli:main" not in entries or "msgd = msg.daemon:main" not in entries:
        raise SystemExit("missing msg or msgd entry point")

with open_tar(sdists[0]) as sdist:
    names = set(sdist.getnames())
    for relative in (
        "src/msg/data/system/AGENTS.md",
        "docs/DEPLOYMENT.md",
        "deploy/msgd.service",
        "tests/test_system_rules.py",
        "conformance/test_transports.py",
    ):
        if not any(name.endswith("/" + relative) for name in names):
            raise SystemExit(f"missing sdist file: {relative}")
    if any(name.endswith(("/setup.py", "/MANIFEST.in")) for name in names):
        raise SystemExit("legacy build files present in sdist")

print("package artifacts contain the expected code, rules, data, and entry points")
