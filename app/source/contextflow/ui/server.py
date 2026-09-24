"""ui/server.py

標準ライブラリの `http.server` だけで動くローカル GUI のサーバ。

- `127.0.0.1` 固定でバインドする（外部から触られないようにするため）
- 起動時にトークンを生成し、GET 以外は `X-CF-Token` ヘッダで照合する
- `Host` / `Origin` が localhost かを検査する
- DB 接続はリクエストごとに `api.handle` の中で開いて閉じる
  （`ThreadingHTTPServer` で複数スレッドから触るため、接続を共有しない）
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import sys
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from contextflow.config import AppConfig
from contextflow.ui import api, collector_runner

# 画面ファイルの置き場所。未作成でも import は壊さない（要求時に 404 を返す）
STATIC_DIR = Path(__file__).resolve().parent / "static"

# index.html の中でトークンを埋め込む目印
TOKEN_PLACEHOLDER = "<!--CF_TOKEN-->"

# バインドを許すホスト名。0.0.0.0 などは拒否する
ALLOWED_BIND_HOSTS = ("127.0.0.1", "localhost")

# database is locked のときの短いリトライ（collect が5秒ごとに書き込むため）
RETRY_COUNT = 3
RETRY_WAIT_SEC = 0.1

# 受け取る本文の上限。画面から送るのは小さな JSON だけなので十分
_MAX_BODY_BYTES = 1_000_000

# 拡張子 -> Content-Type。画面で使うものだけ持つ
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


# ---------------------------------------------------------------------------
# サーバの生成
# ---------------------------------------------------------------------------


def create_server(
    config: AppConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: Optional[str] = None,
) -> tuple[ThreadingHTTPServer, str]:
    """サーバとトークンを作って返す（まだ待ち受けは始めない）。"""
    if host not in ALLOWED_BIND_HOSTS:
        raise ValueError(
            f"このサーバは 127.0.0.1 固定でバインドする。指定された host は使えない: {host}"
            "（0.0.0.0 や LAN の IP を指定すると外部から操作されうるため）"
        )

    issued = token or secrets.token_urlsafe(16)
    # port=0 を渡すと OS が空きポートを割り当てる。実際に bind したポートを
    # Origin の検査へ使うため、ハンドラは bind の後に組み立てる。
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(config, issued, port))
    bound_port = server.server_address[1]
    if bound_port != port:
        server.RequestHandlerClass = _make_handler(config, issued, bound_port)
    server.daemon_threads = True
    return server, issued


def serve(
    config: AppConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    """サーバを起動してブロックする。Ctrl+C で終了。戻り値は終了コード。"""
    try:
        server, token = create_server(config, host=host, port=port)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ポート {port} を開けない: {exc}", file=sys.stderr)
        return 1

    port = server.server_address[1]  # port=0 のときは実際に割り当てられた番号
    url = f"http://{host}:{port}/?token={token}"
    print(f"contextflow UI を起動: {url}")
    print("終了は Ctrl+C。トークンは起動のたびに変わる。")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            # ブラウザが開けなくてもサーバは動かす
            print("ブラウザを自動で開けなかった。上の URL を手で開く。", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了する。")
    finally:
        # UI を終了したら収集スレッドも確実に止め、心拍が残らないようにする
        collector_runner.stop()
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# ハンドラ
# ---------------------------------------------------------------------------


def _make_handler(config: AppConfig, token: str, port: int):
    """config・トークン・ポートを閉じ込めたハンドラクラスを作る。"""

    class Handler(BaseHTTPRequestHandler):
        """1リクエストぶんの処理。状態は持たない。"""

        server_version = "contextflow-ui"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # --- ログ（トークン・クエリは出さない） ---

        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            path = urlparse(self.path).path
            sys.stderr.write(f"[cf-ui] {self.command} {path} {code}\n")

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("[cf-ui] " + (fmt % args) + "\n")

        # --- メソッドごとの入口 ---

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._handle("PUT")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

        # --- 本体 ---

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            # 1. Host の検査（DNS rebinding 対策）
            if not _host_allowed(self.headers.get("Host")):
                self._drain_body()
                self._send_json(403, {"error": "localhost 以外の Host からは受け付けない"})
                return

            # 2. Origin の検査（他サイトからの書き込み対策）
            origin = self.headers.get("Origin")
            if origin and not _origin_allowed(origin, port):
                self._drain_body()
                self._send_json(403, {"error": "localhost 以外の Origin からは受け付けない"})
                return

            # 3. トークンの検査（GET 以外）
            if method != "GET" and not _token_ok(self.headers.get("X-CF-Token"), token):
                self._drain_body()
                self._send_json(403, {"error": "トークンが違う。画面を開き直す"})
                return

            if path.startswith("/api/"):
                self._handle_api(method, path, parsed.query)
                return

            if method != "GET":
                self._send_json(405, {"error": "このパスは GET だけ受け付ける"})
                return

            self._handle_static(path)

        def _handle_api(self, method: str, path: str, raw_query: str) -> None:
            """API 要求を `api.handle` へ委譲する。"""
            query = {key: values[0] for key, values in parse_qs(raw_query).items()}
            try:
                body = self._read_body()
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return

            # collect と同時に書き込むと locked になりうるので短くリトライする
            last_error: Optional[sqlite3.OperationalError] = None
            for attempt in range(RETRY_COUNT):
                try:
                    status, payload = api.handle(method, path, query, body, config)
                    self._send_json(status, payload)
                    return
                except sqlite3.OperationalError as exc:
                    last_error = exc
                    if attempt < RETRY_COUNT - 1:
                        time.sleep(RETRY_WAIT_SEC)
                except Exception:
                    traceback.print_exc()
                    self._send_json(500, {"error": "サーバ内部でエラーが発生した"})
                    return

            sys.stderr.write(f"[cf-ui] DB がロックされている: {last_error}\n")
            self._send_json(
                503, {"error": "DB が使用中で書き込めない。少し待ってからやり直す"}
            )

        def _handle_static(self, path: str) -> None:
            """`static/` 配下のファイルを返す。外へ出るパスは 403。"""
            target = _resolve_static(path)
            if target is None:
                self._send_json(403, {"error": "許可されていないパス"})
                return

            if not target.is_file():
                self._send_notfound_page(target)
                return

            if target.name == "index.html":
                html = target.read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, token)
                self._send_bytes(200, html.encode("utf-8"), _CONTENT_TYPES[".html"])
                return

            content_type = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self._send_bytes(200, target.read_bytes(), content_type)

        def _send_notfound_page(self, target: Path) -> None:
            """画面ファイルが無いことを日本語で伝える。"""
            message = (
                f"画面ファイルが見つからない: {target.name}"
                f"（{STATIC_DIR} に index.html / app.css / app.js を置く）"
            )
            if target.suffix.lower() == ".html":
                body = f"<!doctype html><meta charset=\"utf-8\"><p>{message}</p>"
                self._send_bytes(404, body.encode("utf-8"), _CONTENT_TYPES[".html"])
            else:
                self._send_json(404, {"error": message})

        # --- 入出力 ---

        def _drain_body(self) -> None:
            """本文を読み捨てる。

            HTTP/1.1 は持続接続が既定のため、本文を残したまま応答すると、
            同じ接続で来る次のリクエストが未読の本文から読み始められて壊れる。
            検査で早期に返すときは必ず呼ぶ。
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            remaining = max(0, min(length, _MAX_BODY_BYTES))
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

        def _read_body(self) -> dict:
            """本体を JSON として読む。空なら空 dict。"""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("Content-Length が不正") from None
            if length <= 0:
                return {}
            if length > _MAX_BODY_BYTES:
                self._drain_body()
                raise ValueError("本体が大きすぎる")
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("本体が JSON として読めない") from None
            if not isinstance(parsed, dict):
                raise ValueError("本体は JSON オブジェクトで送る")
            return parsed

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, body, "application/json; charset=utf-8")

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    return Handler


# ---------------------------------------------------------------------------
# 検査のヘルパ
# ---------------------------------------------------------------------------


def _strip_port(value: str) -> str:
    """'127.0.0.1:8765' から host 部分だけを取り出す。"""
    text = value.strip()
    if text.startswith("["):  # IPv6 の [::1]:8765 形式
        return text.partition("]")[0].lstrip("[")
    return text.rsplit(":", 1)[0] if ":" in text else text


def _host_allowed(host_header: Optional[str]) -> bool:
    """`Host` が localhost を指しているか。"""
    if not host_header:
        return False
    return _strip_port(host_header) in ("127.0.0.1", "localhost", "::1")


def _origin_allowed(origin: str, port: int) -> bool:
    """`Origin` が自分自身か。"""
    allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    return origin.strip() in allowed


def _token_ok(given: Optional[str], expected: str) -> bool:
    """トークンを時間一定で比較する。"""
    if not given:
        return False
    return secrets.compare_digest(given, expected)


def _resolve_static(path: str) -> Optional[Path]:
    """URL パスを `static/` 配下の実ファイルへ解決する。

    `static/` の外へ出るパス（`..` など）は None を返す（呼び出し側で 403）。
    `%2e%2e` のようにエンコードされた形も同じ扱いにするため、先に復号する。
    """
    relative = unquote(path).lstrip("/") or "index.html"
    if "\x00" in relative:
        return None
    if relative.endswith("/"):
        relative += "index.html"
    try:
        base = STATIC_DIR.resolve()
        target = (STATIC_DIR / relative).resolve()
    except OSError:
        return None
    if target != base and base not in target.parents:
        return None
    return target
