#!/usr/bin/env python3
"""Cloud encode entry point.

Step 2 of the bash-to-Python migration: a pure shim that forwards all
arguments to cloud_encode.sh. Gets replaced wholesale (not phase by
phase) when encoder/cloud/* is ready.
"""
import os
import sys

BASH_SCRIPT = "/app/scripts/cloud_encode.sh"


def main() -> None:
    os.execvp(BASH_SCRIPT, [BASH_SCRIPT, *sys.argv[1:]])


if __name__ == "__main__":
    main()
