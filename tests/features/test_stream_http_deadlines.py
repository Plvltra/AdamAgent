"""真实TCP和单调时钟验收，不以假时钟代替网络持续心跳。"""

import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from adam.config.settings import LLMConfig
from adam.contracts.errors import FailureCode, LLMError
from adam.llm.client import LLMClient
from tests.features.test_streaming import DONE, event


@contextmanager
def streaming_server(mode):
    """只绑定回环地址；清理时唤醒发送线程并关闭所有服务资源。"""
    stop = threading.Event()
    finished = threading.Event()
    observations = {"requests": 0, "sent": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            observations["requests"] += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                if mode == "disconnect":
                    self.wfile.write(event({"content": "partial"}))
                    self.wfile.flush()
                    return
                count = 8 if mode == "slow" else 100
                for _ in range(count):
                    data = b": heartbeat\n\n" if mode == "heartbeat" else event({"content": "a"})
                    self.wfile.write(data)
                    self.wfile.flush()
                    observations["sent"] += 1
                    if stop.wait(0.25):
                        return
                self.wfile.write(
                    event(reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 8}) + DONE
                )
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                self.close_connection = True
                finished.set()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", observations
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
        if observations["requests"]:
            assert finished.wait(3)


@pytest.mark.parametrize("mode", ["heartbeat", "slow", "disconnect", "total"])
def test_real_http_stream_deadlines_and_disconnect(mode):
    with streaming_server(mode) as (url, seen), httpx.Client(trust_env=False) as http:
        client = LLMClient(
            LLMConfig(
                url,
                "test",
                "test-only",
                timeout_seconds=5,
                semantic_idle_seconds=1.5,
                stream_total_seconds=1.5 if mode == "total" else 8,
            ),
            http,
        )
        started = time.monotonic()
        if mode == "slow":
            reply = client.complete([{"role": "user", "content": "hello"}])
            assert reply.message["content"] == "a" * 8
            assert reply.usage == (3, 8)
            assert time.monotonic() - started >= 1.5
        else:
            with pytest.raises(LLMError) as failed:
                client.complete([{"role": "user", "content": "hello"}])
            expected = (
                FailureCode.INVALID_RESPONSE if mode == "disconnect" else FailureCode.API_TIMEOUT
            )
            assert failed.value.code == expected and failed.value.usage is None
            if mode != "disconnect":
                assert 1.4 <= time.monotonic() - started < 5
                assert seen["sent"] >= 2
        assert seen["requests"] == 1
