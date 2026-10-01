"""Generate a portable animated SVG; no account-specific assets are bundled."""

import argparse
import secrets
from pathlib import Path

from msg.core.profile_art import generate_svg

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('kind', choices=('avatar', 'background'))
parser.add_argument('output', type=Path)
parser.add_argument('--seed', default=None, help='Reuse a seed to reproduce the same artwork.')
args = parser.parse_args()
args.output.write_text(generate_svg(args.seed or secrets.token_hex(16), args.kind))
