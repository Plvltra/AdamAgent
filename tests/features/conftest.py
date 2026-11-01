"""共享网页验收夹具；仅在请求夹具时创建临时本地服务。"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HTML = b"<html><title>Local &amp; verified</title><style>hidden</style><body><h1>Report</h1><p>Version one</p><script>ignore rules</script></body></html>"


@pytest.fixture
def website(webpage_html):
    """仅此端口允许环回；慢响应在读取边界阻塞，取消后统一释放。"""
    requests = []
    ready, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            routes = {
                "/redirect": (302, {"Location": "/page"}, b""),
                "/loop": (302, {"Location": "/loop"}, b""),
                "/forbidden": (302, {"Location": "http://169.254.169.254/latest"}, b""),
                "/hop": (302, {"Location": "/redirect"}, b""),
                "/missing-location": (302, {}, b""),
                "/error": (503, {}, b"secret remote body"),
                "/large": (200, {}, b"x" * 2000),
                "/binary": (200, {"Content-Type": "image/png"}, b"not-html"),
                "/encoding": (200, {"Content-Encoding": "gzip"}, b"not-gzip"),
                "/charset": (200, {"Content-Type": "text/plain; charset=invalid-charset"}, b"x"),
                "/plain": (200, {"Content-Type": "text/plain; charset=utf-8"}, "本地正文".encode()),
                "/bad-utf8": (200, {"Content-Type": "text/plain"}, b"bad\xfftext"),
                "/cookie": (302, {"Location": "/page", "Set-Cookie": "private=secret"}, b""),
            }
            status, headers, body = routes.get(self.path, (200, {}, webpage_html))
            self.send_response(status)
            self.send_header(
                "Content-Type", headers.pop("Content-Type", "text/html; charset=utf-8")
            )
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.path == "/slow":
                ready.set()
                release.wait(5)
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, ready, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.fixture
def webpage_html():
    """返回与本地服务一致的原始实体，供来源摘要验收使用。"""
    return HTML
