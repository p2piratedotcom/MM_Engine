from __future__ import annotations

import hmac
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .kdf import KdfRpcClient
from .kdf_runtime import KdfBinaryManager, validate_runtime_config


class KdfSupervisorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KdfSupervisorStatus:
    state: str
    managed: bool
    rpc_reachable: bool
    version: str | None
    pid: int | None
    started_at: int | None
    binary_sha256: str | None
    last_error: str | None


class KdfSupervisor:
    """Owns one verified KDF process and never stops unrelated processes."""

    def __init__(
        self,
        *,
        client: KdfRpcClient,
        manifest_path: str | Path,
        config_path: str | Path,
        coins_path: str | Path,
        state_path: str | Path,
        log_path: str | Path,
        start_timeout: float = 30.0,
    ) -> None:
        if start_timeout <= 0:
            raise ValueError("KDF start timeout must be positive")
        self.client = client
        self.manager = KdfBinaryManager(manifest_path)
        self.config_path = Path(config_path).resolve()
        self.coins_path = Path(coins_path).resolve()
        self.state_path = Path(state_path).resolve()
        self.log_path = Path(log_path).resolve()
        self.start_timeout = start_timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._started_at: int | None = None
        self._binary_sha256: str | None = None
        self._last_error: str | None = None
        self._lock = threading.RLock()

    def start(self) -> KdfSupervisorStatus:
        with self._lock:
            owned_pid = self._owned_pid()
            if owned_pid is not None:
                return self.status()
            reachable, version = self._rpc_version()
            if reachable:
                raise KdfSupervisorError(
                    "KDF RPC is already reachable but the process is not owned by this supervisor"
                )
            config = self._validated_config()
            verification = self.manager.verify()
            self._verify_official_release()
            if not self.coins_path.is_file():
                raise KdfSupervisorError("pinned KDF coins file is missing")
            runtime_dir = Path(str(config["userhome"])).resolve()
            runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            log_stream = self.log_path.open("ab", buffering=0)
            os.chmod(self.log_path, 0o600)
            environment = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "MM_CONF_PATH": str(self.config_path),
                "MM_COINS_PATH": str(self.coins_path),
                "RUST_LOG": "info",
            }
            try:
                process = subprocess.Popen(
                    [str(self.manager.binary_path)],
                    cwd=runtime_dir,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                log_stream.close()
            self._process = process
            self._started_at = int(time.time())
            self._binary_sha256 = verification.sha256
            self._write_state(process.pid, runtime_dir)

        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._record_failure(f"KDF exited with code {process.returncode}")
                self._clear_state()
                raise KdfSupervisorError(self._last_error or "KDF exited")
            reachable, _ = self._rpc_version()
            if reachable:
                with self._lock:
                    self._last_error = None
                return self.status()
            time.sleep(0.2)
        self._record_failure("KDF RPC did not become ready before timeout")
        self.stop()
        raise KdfSupervisorError(self._last_error or "KDF start timed out")

    def stop(self) -> KdfSupervisorStatus:
        with self._lock:
            pid = self._owned_pid()
            if pid is None:
                self._clear_state()
                return self.status()
        try:
            self.client.legacy("stop")
        except Exception:
            pass
        if not self._wait_stopped(pid, 15.0):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if not self._wait_stopped(pid, 5.0):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._wait_stopped(pid, 5.0)
        with self._lock:
            if self._process is not None:
                try:
                    self._process.wait(timeout=0)
                except (subprocess.TimeoutExpired, ChildProcessError):
                    pass
            self._process = None
            self._started_at = None
            self._clear_state()
        return self.status()

    def restart(self) -> KdfSupervisorStatus:
        self.stop()
        return self.start()

    def status(self) -> KdfSupervisorStatus:
        with self._lock:
            pid = self._owned_pid()
            reachable, version = self._rpc_version()
            if pid is not None:
                state = "RUNNING" if reachable else "STARTING"
            elif reachable:
                state = "EXTERNAL"
            else:
                state = "STOPPED"
            persisted = self._read_state()
            started_at = self._started_at
            if started_at is None and pid is not None:
                value = persisted.get("started_at")
                started_at = int(value) if isinstance(value, int) else None
            digest = self._binary_sha256
            if digest is None and pid is not None:
                value = persisted.get("binary_sha256")
                digest = str(value) if value else None
            return KdfSupervisorStatus(
                state=state,
                managed=pid is not None,
                rpc_reachable=reachable,
                version=version,
                pid=pid,
                started_at=started_at,
                binary_sha256=digest,
                last_error=self._last_error,
            )

    def payload(self) -> dict[str, Any]:
        return asdict(self.status())

    def _validated_config(self) -> Mapping[str, Any]:
        try:
            if self.config_path.stat().st_mode & 0o077:
                raise KdfSupervisorError("KDF config permissions must be 0600")
            loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
        except KdfSupervisorError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise KdfSupervisorError("cannot read the KDF config") from exc
        if not isinstance(loaded, Mapping):
            raise KdfSupervisorError("KDF config must be a JSON object")
        problems = validate_runtime_config(self.config_path)
        if problems:
            raise KdfSupervisorError("invalid KDF config: " + "; ".join(problems))
        configured_password = str(loaded.get("rpc_password", ""))
        if not hmac.compare_digest(configured_password, self.client.userpass):
            raise KdfSupervisorError("KDF RPC password does not match the selected config")
        parsed = urlparse(self.client.rpc_url)
        if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise KdfSupervisorError("managed KDF RPC URL must be loopback")
        if parsed.port != int(loaded.get("rpcport", 7783)):
            raise KdfSupervisorError("KDF RPC URL port does not match the selected config")
        return loaded

    def _verify_official_release(self) -> None:
        manifest = self.manager.manifest
        checks = manifest.get("verification")
        if not isinstance(checks, Mapping):
            raise KdfSupervisorError("KDF provenance checks are missing")
        if manifest.get("project") == "GLEECBTC/komodo-defi-framework":
            required = (
                "github_asset_digest_matches",
                "zip_integrity_ok",
                "detached_signature_valid",
                "signed_checksum_valid",
            )
            if all(checks.get(key) is True for key in required):
                return
        elif manifest.get("project") == "ShorelineCrypto/komodo-defi-framework":
            # This release has no detached asset signature. Accept only the
            # exact reviewed artifact, never an arbitrary unsigned manifest.
            source = manifest.get("source", {})
            binary = manifest.get("binary", {})
            artifact = manifest.get("artifact", {})
            release = manifest.get("release", {})
            if (
                isinstance(source, Mapping)
                and isinstance(binary, Mapping)
                and isinstance(artifact, Mapping)
                and isinstance(release, Mapping)
                and release.get("tag") == "v2.7.0-beta"
                and source.get("commit") == "968f32a6bccf20f286d1b8e2520b62ebe769522b"
                and source.get("commit_signature_verified_by_github") is True
                and artifact.get("sha256") == "cf80e5d5ae78605d6f0f6a806aa9ae5b83bbee0ef79ccd5ad85022ae2a5d7d27"
                and binary.get("sha256") == "bd171eeee7a1e0d43b070c8ba6ba60a845a26b3db0ef57ca25a394a2b6c02129"
                and checks.get("github_asset_digest_matches") is True
                and checks.get("published_checksum_matches") is True
                and checks.get("zip_integrity_ok") is True
            ):
                return
        raise KdfSupervisorError("KDF provenance checks are incomplete")

    def _rpc_version(self) -> tuple[bool, str | None]:
        try:
            result = self.client.legacy("version")
        except Exception:
            return False, None
        return True, str(result)

    def _owned_pid(self) -> int | None:
        state = self._read_state()
        value = state.get("pid")
        if not isinstance(value, int) or value <= 1:
            return None
        proc = Path(f"/proc/{value}")
        if not proc.exists():
            self._clear_state()
            return None
        try:
            executable = (proc / "exe").resolve()
            cwd = (proc / "cwd").resolve()
            expected_cwd = Path(str(state["runtime_dir"])).resolve()
            expected_binary = Path(str(state["binary_path"])).resolve()
            expected_config = Path(str(state["config_path"])).resolve()
            environment = (proc / "environ").read_bytes().split(b"\0")
        except (OSError, KeyError, TypeError):
            return None
        expected_config_entry = f"MM_CONF_PATH={self.config_path}".encode("utf-8")
        if (
            executable != self.manager.binary_path
            or executable != expected_binary
            or cwd != expected_cwd
            or expected_config != self.config_path
            or expected_config_entry not in environment
        ):
            return None
        return value

    def _write_state(self, pid: int, runtime_dir: Path) -> None:
        payload = {
            "pid": pid,
            "runtime_dir": str(runtime_dir),
            "config_path": str(self.config_path),
            "binary_path": str(self.manager.binary_path),
            "binary_sha256": self._binary_sha256,
            "started_at": self._started_at,
        }
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.state_path.parent,
            prefix=f".{self.state_path.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, self.state_path)
        os.chmod(self.state_path, 0o600)

    def _read_state(self) -> Mapping[str, Any]:
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, Mapping) else {}

    def _clear_state(self) -> None:
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass

    def _record_failure(self, message: str) -> None:
        with self._lock:
            self._last_error = message

    def _wait_stopped(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.pid == pid:
                if self._process.poll() is not None:
                    return True
            else:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                except PermissionError:
                    return False
            time.sleep(0.1)
        return False
