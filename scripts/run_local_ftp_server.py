#!/usr/bin/env python3
"""
本机 FTP 服务（联调 STORAGE_BACKEND=ftp + MySQL/TiDB）。

  pip install '.[ftp-test]'   # 安装 pyftpdlib
  python scripts/run_local_ftp_server.py

根目录默认 ``<仓库>/.runtime/ftp_home``，与 ``.env`` 中 ``FTP_ROOT=/dataset`` 组合后，
客户端逻辑路径仍为 ``/dataset/...``（见 ``backend/app/storage/backend.py``）。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

DEFAULT_PORT = int(os.environ.get("FTP_PORT", "2121"))
DEFAULT_USER = os.environ.get("FTP_USER", "dataset")
DEFAULT_PASSWORD = os.environ.get("FTP_PASSWORD", "change-me")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local FTP for dataset backend integration tests.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".runtime" / "ftp_home",
        help="Physical home directory for the FTP user",
    )
    args = parser.parse_args()

    try:
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.servers import FTPServer
    except ImportError as e:  # pragma: no cover
        raise SystemExit("请安装 pyftpdlib：pip install '.[ftp-test]'") from e

    args.home.mkdir(parents=True, exist_ok=True)
    # Align with FTP_ROOT=/dataset (.env.example): logical namespace lives under ~/dataset
    (args.home / "dataset").mkdir(parents=True, exist_ok=True)
    authorizer = DummyAuthorizer()
    authorizer.add_user(args.user, args.password, str(args.home), perm="elradfmwMT")

    handler = FTPHandler
    handler.authorizer = authorizer

    server = FTPServer((args.host, args.port), handler)
    print(
        f"FTP listening on ftp://{args.user}:***@{args.host}:{args.port}  home={args.home}\n"
        f"后端配置示例: FTP_HOST={args.host} FTP_PORT={args.port} "
        f"FTP_USER={args.user} FTP_PASSWORD=<secret> STORAGE_BACKEND=ftp"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
