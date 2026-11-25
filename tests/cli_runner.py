"""Invoke the real CLI without paying for a Python process per command."""

from contextlib import chdir, nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
import os
from subprocess import CompletedProcess
from unittest.mock import patch

from phalanx.__main__ import main


def run_cli(arguments, *, env=None, cwd=None):
    argv = ["phalanx", *arguments]
    stdout, stderr = StringIO(), StringIO()
    returncode = 0
    with (patch("sys.argv", argv), patch.dict(os.environ, env or {}),
          chdir(cwd) if cwd is not None else nullcontext(),
          redirect_stdout(stdout), redirect_stderr(stderr)):
        try:
            main()
        except SystemExit as error:
            if error.code is None:
                returncode = 0
            elif isinstance(error.code, int):
                returncode = error.code
            else:
                print(error.code, file=stderr)
                returncode = 1
    return CompletedProcess(argv, returncode, stdout.getvalue(), stderr.getvalue())
