"""Command line entry point: ``data-viewer [PATH]``."""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import webbrowser
from pathlib import Path

os.environ.setdefault("LANCE_LOG", "error")


def _free_port(host: str, start: int, tries: int = 30) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
            # Same option uvicorn uses, so sockets lingering in TIME_WAIT don't count as busy.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"端口 {start}-{start + tries - 1} 都被占用，请用 --port 指定其他端口")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="data-viewer",
        description="本地只读数据查看器：Lance / Parquet / CSV / JSON(L) / Arrow / Feather / HDF5 / WebDataset",
    )
    ap.add_argument("path", nargs="?", default=".", help="要打开的数据集或目录（默认：当前目录）")
    ap.add_argument("--root", help="文件浏览的根目录（默认：PATH 是普通目录时为它本身，是数据集时为其上级目录）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本机可访问）")
    ap.add_argument("--port", type=int, default=8765, help="端口（默认 8765，被占用时自动顺延）")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--token", help="访问令牌；监听非本机地址时默认自动生成")
    ap.add_argument("--threads", type=int, help="DuckDB 线程数")
    ap.add_argument("--memory-limit", help="DuckDB 内存上限，例如 8GB")
    ap.add_argument("--timeout", type=float, default=120.0, help="单次查询超时秒数（默认 120）")
    ap.add_argument("--demo", action="store_true", help="生成各种格式的演示数据（存放在 PATH，默认 ~/.cache/data-viewer/demo）并打开")
    args = ap.parse_args(argv)

    from .server import LOOPBACK, AppConfig, create_app
    from .sources import detect

    if args.demo:
        from .demo import make_demo

        demo_dir = Path(args.path).expanduser() if args.path != "." else Path.home() / ".cache" / "data-viewer" / "demo"
        print(f"正在生成演示数据：{demo_dir}")
        args.path = str(make_demo(demo_dir))

    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        print(f"路径不存在：{target}", file=sys.stderr)
        return 2
    fmt = detect(target)
    if args.root:
        root = Path(args.root).expanduser().resolve()
    elif target.is_dir() and fmt is None:
        root = target
    else:
        root = target.parent
    initial = target if fmt else None
    if initial is not None and root not in initial.parents and initial != root:
        print(f"{initial} 不在根目录 {root} 之内", file=sys.stderr)
        return 2

    token = args.token
    if token is None and args.host not in LOOPBACK:
        token = secrets.token_urlsafe(16)

    port = _free_port(args.host, args.port)
    app = create_app(AppConfig(root=root, initial=initial, host=args.host, token=token, threads=args.threads,
                               memory_limit=args.memory_limit, query_timeout=args.timeout))
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    if ":" in shown_host and not shown_host.startswith("["):
        shown_host = f"[{shown_host}]"
    url = f"http://{shown_host}:{port}/" + (f"?token={token}" if token else "")
    print(f"Data Viewer 已启动：{url}", flush=True)
    print(f"根目录：{root}" + (f"\n打开：{initial}" if initial else ""))
    print("按 Ctrl+C 退出", flush=True)
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, (url,)).start()

    import uvicorn

    uvicorn.run(app, host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
