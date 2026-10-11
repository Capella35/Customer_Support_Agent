"""`python -m eval.run`, the name TECHNICAL §6 uses: the same runner as `python -m eval.runner`."""

import asyncio
import sys

from eval.runner import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
