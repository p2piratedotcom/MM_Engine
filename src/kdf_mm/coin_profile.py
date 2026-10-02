from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


PROFILE_SCHEMA_VERSION = 1
MAX_PROFILE_BYTES = 64 * 1024
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,63}$")


class CoinProfileError(ValueError):
    pass


def normalize_tickers(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        ticker = str(value).strip().upper()
        if not _TICKER.fullmatch(ticker):
            raise CoinProfileError(f"ticker non valido: {ticker or '<vuoto>'}")
        if ticker not in result:
            result.append(ticker)
    if not result:
        raise CoinProfileError("il profilo deve contenere almeno una coin")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CoinProfile:
    tickers: tuple[str, ...]
    saved_at: int

    def payload(self, *, path: Path | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "exists": True,
            "schema_version": PROFILE_SCHEMA_VERSION,
            "tickers": list(self.tickers),
            "saved_at": self.saved_at,
        }
        if path is not None:
            result["path"] = str(path)
        return result


class CoinProfileStore:
    """Private, atomic persistence for the user's KDF activation set."""

    def __init__(
        self, path: str | Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.path = Path(path).resolve()
        self._clock = clock

    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"exists": False, "tickers": [], "path": str(self.path)}
        return self.load().payload(path=self.path)

    def load(self) -> CoinProfile:
        try:
            if self.path.stat().st_size > MAX_PROFILE_BYTES:
                raise CoinProfileError("il profilo coin è troppo grande")
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except CoinProfileError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise CoinProfileError("impossibile leggere il profilo coin") from exc
        if not isinstance(loaded, Mapping):
            raise CoinProfileError("il profilo coin deve essere un oggetto JSON")
        if loaded.get("schema_version") != PROFILE_SCHEMA_VERSION:
            raise CoinProfileError("versione del profilo coin non supportata")
        raw_tickers = loaded.get("tickers")
        if not isinstance(raw_tickers, list):
            raise CoinProfileError("il profilo coin non contiene una lista valida")
        try:
            saved_at = int(loaded.get("saved_at", 0))
        except (TypeError, ValueError) as exc:
            raise CoinProfileError("data del profilo coin non valida") from exc
        if saved_at < 0:
            raise CoinProfileError("data del profilo coin non valida")
        return CoinProfile(normalize_tickers(raw_tickers), saved_at)

    def save(self, tickers: Iterable[object]) -> CoinProfile:
        profile = CoinProfile(normalize_tickers(tickers), int(self._clock()))
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as stream:
                temporary_name = stream.name
                os.chmod(temporary_name, 0o600)
                json.dump(profile.payload(), stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return profile
