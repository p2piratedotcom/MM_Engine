from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable


class SecretServiceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MexcCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ValueError("both MEXC credentials are required")


@dataclass(frozen=True, slots=True)
class GateCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ValueError("both Gate credentials are required")


class LinuxSecretService:
    """Stores CEX credentials in the user's Secret Service keyring."""

    def __init__(
        self,
        *,
        profile: str = "default",
        executable: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        if not profile or len(profile) > 64:
            raise ValueError("invalid CEX keyring profile")
        resolved = executable or shutil.which("secret-tool")
        if not resolved:
            raise SecretServiceError(
                "secret-tool is unavailable; install the libsecret tools on Zorin"
            )
        self.profile = profile
        self.executable = resolved
        self._runner = runner or subprocess.run

    def _venue_kinds(self, venue: str):
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,31}', venue):
            raise ValueError('invalid credential venue')
        prefix = '' if venue == 'MEXC' else venue.lower() + '-'
        return prefix + 'api-key', prefix + 'api-secret'

    def load(self, venue: str):
        kinds = self._venue_kinds(venue)
        values = [self._lookup(kind) for kind in kinds]
        if not all(values):
            raise SecretServiceError(f'{venue} credentials are incomplete')
        return MexcCredentials(*values)

    def store(self, venue: str, credentials):
        for kind, value in zip(self._venue_kinds(venue), (credentials.api_key, credentials.api_secret)):
            self._store(kind, value, f'P2Pirate {venue} Spot credential')

    def load_mexc(self) -> MexcCredentials:
        api_key = self._lookup("api-key")
        api_secret = self._lookup("api-secret")
        if not api_key or not api_secret:
            raise SecretServiceError(
                f"MEXC credentials are incomplete in keyring profile {self.profile}"
            )
        return MexcCredentials(api_key=api_key, api_secret=api_secret)

    def store_mexc(self, credentials: MexcCredentials) -> None:
        self._store("api-key", credentials.api_key, "KDF MM MEXC API key")
        self._store("api-secret", credentials.api_secret, "KDF MM MEXC API secret")

    def load_gate(self) -> GateCredentials:
        api_key = self._lookup("gate-api-key")
        api_secret = self._lookup("gate-api-secret")
        if not api_key or not api_secret:
            raise SecretServiceError(
                f"Gate credentials are incomplete in keyring profile {self.profile}"
            )
        return GateCredentials(api_key=api_key, api_secret=api_secret)

    def store_gate(self, credentials: GateCredentials) -> None:
        self._store("gate-api-key", credentials.api_key, "KDF MM Gate API key")
        self._store("gate-api-secret", credentials.api_secret, "KDF MM Gate API secret")

    def _attributes(self, kind: str) -> list[str]:
        return [
            "application",
            "kdf-mm",
            "profile",
            self.profile,
            "credential",
            kind,
        ]

    def _lookup(self, kind: str) -> str:
        result = self._runner(
            [self.executable, "lookup", *self._attributes(kind)],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.rstrip("\r\n")

    def _store(self, kind: str, value: str, label: str) -> None:
        if not value:
            raise ValueError("cannot store an empty CEX credential")
        result = self._runner(
            [
                self.executable,
                "store",
                f"--label={label}",
                *self._attributes(kind),
            ],
            input=value,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "Secret Service rejected the request"
            raise SecretServiceError(f"cannot store {kind}: {detail}")
