"""Connector credentials: env var BRAIN_<NAME>_TOKEN, else the OS keyring (Keychain, Secret Service, Credential Locker)."""

import getpass
import os

import keyring

SERVICE = "brain"


def get(name: str) -> str | None:
    return os.environ.get(f"BRAIN_{name.upper()}_TOKEN") or keyring.get_password(SERVICE, name)


def prompt(name: str) -> None:
    keyring.set_password(SERVICE, name, getpass.getpass(f"{name} token: "))
