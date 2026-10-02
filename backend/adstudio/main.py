"""Launcher and admin CLI.

  adstudio                     start the app (prompts for the library passphrase if the library is encrypted)
  adstudio encrypt             encrypt the library database with a passphrase (close the app first)
  adstudio decrypt             remove database encryption
  adstudio unlock --remember   check the passphrase and cache the derived key in the OS keychain
  adstudio forget-key          remove the cached key from the OS keychain
"""
import argparse
import getpass
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from .core.config import Config
from .core.errors import AppError
from .storage import crypt


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _new_passphrase() -> str:
    env = os.environ.get("ADSTUDIO_NEW_PASSPHRASE")
    if env:
        return env
    first = getpass.getpass("New library passphrase: ")
    if first != getpass.getpass("Repeat passphrase: "):
        raise AppError("Passphrases do not match", code="mismatch")
    return first


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def _extra_hosts(value: str) -> set[str]:
    """ADSTUDIO_ALLOWED_HOSTS="nas.local,192.168.1.20" -> extra Host header names (no ports)."""
    return {h.strip().lower() for h in value.split(",") if h.strip()}


def serve(args, config: Config) -> None:
    import uvicorn

    from .api.app import create_app
    from .core.container import build_container

    host = args.host or os.environ.get("ADSTUDIO_HOST") or "127.0.0.1"
    exposed = not _is_loopback(host)
    token = os.environ.get("ADSTUDIO_ACCESS_TOKEN", "")
    if exposed and len(token) < 24:  # fail closed: never listen beyond loopback without a secret
        raise AppError("Listening on a non-loopback address (Docker, LAN) requires ADSTUDIO_ACCESS_TOKEN, "
                       "a secret of at least 24 characters. Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\"",
                       code="access_token_required")
    key = None
    if crypt.is_encrypted(config.data_root):
        key = crypt.get_key(config.data_root, interactive=sys.stdin.isatty(), remember=args.remember)
    port = args.port or config.port or int(os.environ.get("ADSTUDIO_PORT") or 0) or (8765 if exposed else _free_port())
    container = build_container(config, key=key)
    app = create_app(container)
    if exposed:
        container.auth.access_token = token
        container.auth.allowed_hosts |= _extra_hosts(os.environ.get("ADSTUDIO_ALLOWED_HOSTS", ""))
        roots = [(config.data_root / "inbox").resolve()]
        roots += [Path(r).resolve() for r in os.environ.get("ADSTUDIO_IMPORT_ROOTS", "").split(os.pathsep) if r]
        app.state.import_roots = roots  # the server-side path import may only read these folders
        print(f"AI Document Studio listening on {host}:{port}. Open http://localhost:{port}/?t=<your ADSTUDIO_ACCESS_TOKEN> "
              f"once per browser to sign in. Traffic is plain HTTP: put a TLS reverse proxy in front for anything beyond a trusted network.",
              flush=True)
    else:
        url = f"http://127.0.0.1:{port}/?t={container.auth.issue_one_time()}"
        print(f"AI Document Studio running. Open: {url}", flush=True)
        if not args.no_browser:
            threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="adstudio", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--host", default=None, help="address to listen on (default 127.0.0.1; anything else needs ADSTUDIO_ACCESS_TOKEN)")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--remember", action="store_true", help="cache the derived key in the OS keychain")
    sub = p.add_subparsers(dest="cmd")
    e = sub.add_parser("encrypt", help="encrypt the library database")
    e.add_argument("--delete-plain", action="store_true", help="delete the plaintext copy afterwards (default: keep it)")
    e.add_argument("--remember", action="store_true", dest="remember_enc")
    d = sub.add_parser("decrypt", help="remove database encryption")
    d.add_argument("--delete-encrypted", action="store_true")
    u = sub.add_parser("unlock", help="verify the passphrase (and cache the key with --remember)")
    u.add_argument("--remember", action="store_true", dest="remember_unlock")
    sub.add_parser("forget-key", help="remove the cached key from the OS keychain")
    args = p.parse_args(argv)
    config = Config.load(args.data_dir)
    try:
        if args.cmd is None:
            serve(args, config)
        elif args.cmd == "encrypt":
            res = crypt.encrypt_in_place(config.data_root, _new_passphrase(), keep_plain=not args.delete_plain)
            if args.remember_enc:
                crypt.remember_key(config.data_root, crypt.get_key(config.data_root, passphrase=os.environ.get("ADSTUDIO_NEW_PASSPHRASE") or None,
                                                                  interactive=True))
            print("Database encrypted.", flush=True)
            if res["plaintext_copy"]:
                print(f"A PLAINTEXT copy remains at {res['plaintext_copy']}: delete it once you have checked the app opens.")
            print("Note: original files under files/ are not encrypted; use full-disk encryption (BitLocker) for them.")
        elif args.cmd == "decrypt":
            res = crypt.decrypt_in_place(config.data_root, getpass.getpass("Library passphrase: "), keep_encrypted=not args.delete_encrypted)
            print("Database decrypted.", flush=True)
        elif args.cmd == "unlock":
            crypt.get_key(config.data_root, interactive=True, remember=args.remember_unlock)
            print("Passphrase accepted." + (" Key cached in the OS keychain." if args.remember_unlock else ""))
        elif args.cmd == "forget-key":
            crypt.forget_key(config.data_root)
            print("Cached key removed.")
        return 0
    except AppError as e:
        print(f"error: {e.detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
