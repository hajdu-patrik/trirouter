"""`python -m jev_router <command>` from an old launcher or shim: runs `python -m trirouter`."""
import sys

from trirouter.cli import main

sys.exit(main())
