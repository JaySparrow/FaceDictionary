"""Entry point of the packaged app: import the package so that `facedict/__main__.py` runs
with its package context (relative imports, the sandbox restart and the network guard)."""

import sys

from facedict.__main__ import main

sys.exit(main())
