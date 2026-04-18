#!/usr/bin/env python3
"""Local encode entry point.

Step 2 of the bash-to-Python migration: a pure shim that forwards all
arguments to create_abr_ladder.sh. Subsequent steps replace bash phases
one at a time; eventually the subprocess call disappears entirely.
"""
import os
import sys

BASH_SCRIPT = "/app/scripts/create_abr_ladder.sh"


def main() -> None:
    os.execvp(BASH_SCRIPT, [BASH_SCRIPT, *sys.argv[1:]])


if __name__ == "__main__":
    main()
