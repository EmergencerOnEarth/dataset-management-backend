"""Physical file storage: FTP (production-like) or local directory (pytest / no daemon)."""

from __future__ import annotations

import io
import shutil
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from backend.app.core.config import Settings


class StorageBackend(ABC):
    @abstractmethod
    def mkdir_p(self, remote_dir: str) -> None:
        """Create remote directory and parents (leading / optional)."""

    @abstractmethod
    def put_bytes(self, remote_path: str, data: bytes) -> None:
        """Overwrite file at full remote path under storage root."""

    @abstractmethod
    def put_file_from_path(self, remote_path: str, local_path: Path) -> None:
        """Stream a local file to ``remote_path`` without loading the whole file into memory."""

    @abstractmethod
    def get_bytes(self, remote_path: str) -> bytes:
        """Read full file."""

    @abstractmethod
    def fetch_to_local(
        self,
        remote_path: str,
        local_path: Path,
        *,
        block_callback: Callable[[int], None] | None = None,
    ) -> None:
        """Stream remote file to ``local_path`` (overwrite); avoids loading multi‑GiB zips into RAM."""

    @abstractmethod
    def put_bytes_batch(self, items: list[tuple[str, bytes]]) -> None:
        """Write many files in one transport session (critical for Docker FTP performance).

        ``items`` is a list of ``(remote_path, data)`` pairs.  All paths may
        span multiple directories; the implementation must create parents as
        needed.  Order within the batch is preserved but not guaranteed to be
        atomic.
        """

    @abstractmethod
    def exists(self, remote_path: str) -> bool:
        pass

    @abstractmethod
    def remove_file(self, remote_path: str) -> None:
        pass


def normalize_storage_path(path: str) -> str:
    p = path.strip().replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return p


class LocalMirrorStorage(StorageBackend):
    """
    Maps logical paths under ``<dataset_runtime_dir>/ftp_mirror``.
    Used when ``STORAGE_BACKEND=local``.
    """

    def __init__(self, settings: Settings) -> None:
        self._root: Path = settings.dataset_runtime_dir / "ftp_mirror"
        self._root.mkdir(parents=True, exist_ok=True)

    def _full(self, remote_path: str) -> Path:
        rel = normalize_storage_path(remote_path).lstrip("/")
        return self._root / rel

    def mkdir_p(self, remote_dir: str) -> None:
        self._full(remote_dir).mkdir(parents=True, exist_ok=True)

    def put_bytes(self, remote_path: str, data: bytes) -> None:
        path = self._full(remote_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def put_file_from_path(self, remote_path: str, local_path: Path) -> None:
        path = self._full(remote_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, path)

    def get_bytes(self, remote_path: str) -> bytes:
        return self._full(remote_path).read_bytes()

    def fetch_to_local(
        self,
        remote_path: str,
        local_path: Path,
        *,
        block_callback: Callable[[int], None] | None = None,
    ) -> None:
        src = self._full(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if block_callback is None:
            shutil.copyfile(src, local_path)
            return
        with src.open("rb") as inp, local_path.open("wb") as out:
            while True:
                chunk = inp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                block_callback(len(chunk))

    def put_bytes_batch(self, items: list[tuple[str, bytes]]) -> None:
        for remote_path, data in items:
            self.put_bytes(remote_path, data)

    def exists(self, remote_path: str) -> bool:
        return self._full(remote_path).is_file()

    def remove_file(self, remote_path: str) -> None:
        p = self._full(remote_path)
        if p.is_file():
            p.unlink()


class FtpStorage(StorageBackend):
    """FTP client using stdlib ``ftplib``."""

    def __init__(self, settings: Settings) -> None:
        import ftplib

        self._settings = settings
        self._ftplib = ftplib

    def _path_under_root(self, remote_path: str) -> str:
        """
        逻辑路径（如 ``/dataset/upload/...``）减去 ``FTP_ROOT``（如 ``/dataset``），
        得到相对 FTP 根目录的路径段，避免重复进入 ``dataset/``。
        """
        full = normalize_storage_path(remote_path)
        root = normalize_storage_path(self._settings.ftp_root).rstrip("/") or ""
        if root and (full == root or full.startswith(root + "/")):
            return full[len(root) :].lstrip("/")
        return full.lstrip("/")

    def _cwd_root(self, ftp):
        root = normalize_storage_path(self._settings.ftp_root)
        segments = [s for s in root.split("/") if s]
        ftp.cwd("/")
        for seg in segments:
            try:
                ftp.cwd(seg)
            except Exception:
                try:
                    ftp.mkd(seg)
                except Exception:
                    pass
                ftp.cwd(seg)

    def _connect(self):
        ftp = self._ftplib.FTP()
        # Large merged zips (multi‑GiB) need long RETR; 2h avoids abort mid-stream.
        ftp.connect(self._settings.ftp_host, self._settings.ftp_port, timeout=7200)
        user = self._settings.ftp_user or "anonymous"
        passwd = self._settings.ftp_password or ""
        ftp.login(user, passwd)
        self._cwd_root(ftp)
        return ftp

    def mkdir_p(self, remote_dir: str) -> None:
        """Create path segments relative to configured FTP root."""
        path = self._path_under_root(remote_dir)
        if not path:
            return
        ftp = self._connect()
        try:
            self._cwd_root(ftp)
            start = ftp.pwd()
            for seg in path.split("/"):
                if not seg:
                    continue
                try:
                    ftp.cwd(seg)
                except Exception:
                    ftp.mkd(seg)
                    ftp.cwd(seg)
            ftp.cwd(start)
        finally:
            ftp.quit()

    def put_bytes(self, remote_path: str, data: bytes) -> None:
        rel = self._path_under_root(remote_path)
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        ftp = self._connect()
        try:
            self._cwd_root(ftp)
            if parent:
                for seg in parent.split("/"):
                    if not seg:
                        continue
                    try:
                        ftp.cwd(seg)
                    except Exception:
                        ftp.mkd(seg)
                        ftp.cwd(seg)
            bio = io.BytesIO(data)
            fname = rel.rsplit("/", 1)[-1]
            ftp.storbinary(f"STOR {fname}", bio)
        finally:
            ftp.quit()

    def put_file_from_path(self, remote_path: str, local_path: Path) -> None:
        rel = self._path_under_root(remote_path)
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        ftp = self._connect()
        try:
            self._cwd_root(ftp)
            if parent:
                for seg in parent.split("/"):
                    if not seg:
                        continue
                    try:
                        ftp.cwd(seg)
                    except Exception:
                        ftp.mkd(seg)
                        ftp.cwd(seg)
            fname = rel.rsplit("/", 1)[-1]
            with local_path.open("rb") as f:
                ftp.storbinary(f"STOR {fname}", f)
        finally:
            ftp.quit()

    def get_bytes(self, remote_path: str) -> bytes:
        rel = self._path_under_root(remote_path)
        parent, _, name = rel.rpartition("/")
        ftp = self._connect()
        try:
            self._cwd_root(ftp)
            if parent:
                for seg in parent.split("/"):
                    if not seg:
                        continue
                    ftp.cwd(seg)
            buf = io.BytesIO()
            ftp.retrbinary(f"RETR {name}", buf.write)
            return buf.getvalue()
        finally:
            ftp.quit()

    def _prepare_ftp_transfer(self, ftp) -> None:
        try:
            ftp.encoding = "utf-8"
            ftp.sendcmd("OPTS UTF8 ON")
        except Exception:
            pass
        if ftp.sock is not None:
            ftp.sock.settimeout(300)

    def fetch_to_local(
        self,
        remote_path: str,
        local_path: Path,
        *,
        block_callback: callable[[int], None] | None = None,
    ) -> None:
        rel = self._path_under_root(remote_path)
        parent, _, name = rel.rpartition("/")
        ftp = self._connect()
        try:
            self._prepare_ftp_transfer(ftp)
            self._cwd_root(ftp)
            if parent:
                for seg in parent.split("/"):
                    if not seg:
                        continue
                    ftp.cwd(seg)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with local_path.open("wb") as out:

                def _writer(data: bytes) -> None:
                    if ftp.sock is not None:
                        ftp.sock.settimeout(300)
                    out.write(data)
                    if block_callback is not None:
                        block_callback(len(data))

                ftp.retrbinary(f"RETR {name}", _writer, blocksize=1024 * 1024)
        finally:
            ftp.quit()

    def _ftp_cwd_mkdir(self, ftp, dir_rel: str) -> None:
        """Navigate into ``dir_rel`` (relative to FTP root), creating dirs as needed."""
        self._cwd_root(ftp)
        for seg in dir_rel.split("/"):
            if not seg:
                continue
            try:
                ftp.cwd(seg)
            except Exception:
                ftp.mkd(seg)
                ftp.cwd(seg)

    def put_bytes_batch(self, items: list[tuple[str, bytes]]) -> None:
        """Write all items in a single FTP session, grouping files by directory."""
        if not items:
            return
        from itertools import groupby

        # Group by parent directory so we minimise CWD calls.
        def _parent(path: str) -> str:
            rel = self._path_under_root(path)
            return rel.rsplit("/", 1)[0] if "/" in rel else ""

        sorted_items = sorted(items, key=lambda x: _parent(x[0]))
        ftp = self._connect()
        try:
            current_dir: str | None = None
            for remote_path, data in sorted_items:
                rel = self._path_under_root(remote_path)
                parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                fname = rel.rsplit("/", 1)[-1]
                if parent != current_dir:
                    self._ftp_cwd_mkdir(ftp, parent)
                    current_dir = parent
                ftp.storbinary(f"STOR {fname}", io.BytesIO(data))
        finally:
            ftp.quit()

    def exists(self, remote_path: str) -> bool:
        rel = self._path_under_root(remote_path)
        parent, _, name = rel.rpartition("/")
        ftp = self._connect()
        try:
            self._cwd_root(ftp)
            if parent:
                for seg in parent.split("/"):
                    if not seg:
                        continue
                    try:
                        ftp.cwd(seg)
                    except Exception:
                        # Parent directory does not exist → file cannot exist either
                        return False
            try:
                ftp.voidcmd("TYPE I")
            except Exception:
                pass
            try:
                sz = ftp.size(name)
                if sz is not None and sz >= 0:
                    return True
            except Exception:
                pass
            try:
                names = ftp.nlst()
                basenames = {Path(str(x).replace("\\", "/")).name for x in names}
                if name in names or name in basenames:
                    return True
            except Exception:
                pass
            return False
        finally:
            ftp.quit()

    def remove_file(self, remote_path: str) -> None:
        rel = self._path_under_root(remote_path)
        parent, _, name = rel.rpartition("/")
        ftp = self._connect()
        try:
            self._cwd_root(ftp)
            if parent:
                for seg in parent.split("/"):
                    if not seg:
                        continue
                    ftp.cwd(seg)
            ftp.delete(name)
        finally:
            ftp.quit()


def get_storage(settings: Settings) -> StorageBackend:
    if settings.storage_backend == "ftp":
        return FtpStorage(settings)
    return LocalMirrorStorage(settings)
