"""Provision the operator password for the energy_control broker (interactive).

Run as root by the operator: ``sudo python3 -m energy_control.passwd --user NAME``.
The password is read twice from the terminal (never argv, environment or
files) and stored only as an scrypt digest in a root-only file. Restart
``energy_control`` afterwards to enable the operator socket.
"""
import argparse
import getpass
import os
import pwd
import sys

from .broker import PasswordVerifier
from .operator_broker import PASSWORD_PATH, write_password_file


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", required=True, help="non-root operator account")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise SystemExit("run as root (sudo)")
    try:
        uid = pwd.getpwnam(args.user).pw_uid
    except KeyError:
        raise SystemExit("unknown user")
    if uid == 0:
        raise SystemExit("the operator must be a non-root account")
    if not sys.stdin.isatty():
        raise SystemExit("interactive terminal required")
    first = getpass.getpass("New operator password (12..256 characters): ")
    if first != getpass.getpass("Repeat: "):
        raise SystemExit("passwords differ")
    write_password_file(PasswordVerifier.provision(first), uid)
    print(f"operator password stored for uid {uid} in {PASSWORD_PATH}; "
          "restart energy_control to enable the operator socket")


if __name__ == "__main__":
    main()
