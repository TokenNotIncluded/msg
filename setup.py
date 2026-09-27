"""Copy release-owned rule sources into the wheel without a second checked-in copy."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithSystemRules(build_py):
    def run(self):
        super().run()
        source = Path(__file__).parent / 'docs' / 'system'
        target = Path(self.build_lib) / 'msg' / 'data' / 'system'
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)


setup(cmdclass={'build_py': BuildWithSystemRules})
