import socket
import ssl
from urllib.error import URLError
from urllib.request import HTTPSHandler, OpenerDirector

import pytest

import hf_downloader as hf


def test_https_requires_certificate_and_hostname_verification(tmp_path, monkeypatch):
    task = hf.Downloader(hf.parser().parse_args(["test/model", "-o", str(tmp_path), "--quiet"]))
    contexts = []

    def inspect_open(opener, *args, **kwargs):
        contexts.extend(handler._context for handler in opener.handlers
                        if isinstance(handler, HTTPSHandler))

    monkeypatch.setattr(OpenerDirector, "open", inspect_open)
    task.open("https://example.invalid/api/models/test/model")
    assert len(contexts) == 1
    assert isinstance(contexts[0], hf.truststore.SSLContext)
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname is True


def certificate_error(code):
    error = ssl.SSLCertVerificationError(1, "sensitive-proxy-password-and-signed-url")
    error.verify_code = code
    return error


@pytest.mark.parametrize("reason,expected", [
    (certificate_error(10), "证书已过期"),
    (certificate_error(9), "证书尚未生效"),
    (certificate_error(20), "证书验证失败"),
    (socket.gaierror(socket.EAI_NONAME, "sensitive-host"), "DNS 解析失败"),
    (TimeoutError("sensitive-host"), "超时"),
    (ConnectionRefusedError("sensitive-host"), "拒绝连接"),
    (ConnectionResetError("sensitive-host"), "重置"),
    (ssl.SSLError("sensitive-host"), "TLS 握手"),
    ("Tunnel connection failed: 407 sensitive-proxy-password", "HTTP 407"),
    ("unknown sensitive-signed-url", "网络不可用"),
])
def test_request_failure_explains_reason_without_leaking_credentials(tmp_path, reason, expected):
    task = hf.Downloader(hf.parser().parse_args(["test/model", "-o", str(tmp_path), "--quiet"]))

    class FailedOpener:
        def open(self, *args, **kwargs):
            raise URLError(reason)

    task.local.opener = FailedOpener()
    with pytest.raises(hf.Retryable) as caught:
        task.open("https://example.invalid/api/models/test/model")
    assert expected in str(caught.value)
    assert "sensitive" not in str(caught.value)


def test_unwrapped_certificate_failure_is_also_retryable(tmp_path):
    task = hf.Downloader(hf.parser().parse_args(["test/model", "-o", str(tmp_path), "--quiet"]))

    class FailedOpener:
        def open(self, *args, **kwargs):
            raise certificate_error(10)

    task.local.opener = FailedOpener()
    with pytest.raises(hf.Retryable, match="证书已过期"):
        task.open("https://example.invalid/")
