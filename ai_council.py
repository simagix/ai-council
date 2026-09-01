#!/usr/bin/env python3
"""ai-council launcher.

Lets you run the tool without installing it:

    python ai_council.py "Should I buy 256GB or 512GB?"
    python ai_council.py --version

The implementation lives in the ``ai_council`` package next to this file;
after ``pip install -e .`` the ``ai-council`` console script is also
available and behaves identically.
"""

import sys

from cli import main

if __name__ == "__main__":
    sys.exit(main())
