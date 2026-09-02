#!/usr/bin/env python3
"""ai-council launcher.

Lets you run the tool without installing it:

    python ai_council.py "Should I buy 256GB or 512GB?"
    python ai_council.py --version

The implementation lives in the modules next to this file.
"""

import sys

from cli import main

if __name__ == "__main__":
    sys.exit(main())
