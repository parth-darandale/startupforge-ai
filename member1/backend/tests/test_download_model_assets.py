import importlib.util
from pathlib import Path

import httpx
import pytest


spec = importlib.util.spec_from_file_location(
    "download_model_assets", Path(__file__).resolve().parents[1] / "scripts" / "download_model_assets.py"
)
downloader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(downloader)


class InterruptedStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"def"
        raise httpx.RemoteProtocolError("connection closed")


def transport_for(data, requests, interrupted=None, forbidden=False):
    def handle(request):
        header = request.headers["Range"]
        requests.append(header)
        start, end = map(int, header.removeprefix("bytes=").split("-"))
        if forbidden and len(requests) > 1:
            return httpx.Response(403)
        headers = {"Content-Range": f"bytes {start}-{end}/{len(data)}"}
        if interrupted and header == interrupted and requests.count(header) == 1:
            return httpx.Response(206, headers=headers, stream=InterruptedStream())
        return httpx.Response(206, headers=headers, content=data[start:end + 1])
    return httpx.MockTransport(handle)


def test_resumes_saved_bytes_after_transport_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "CHUNK_SIZE", 12)
    monkeypatch.setattr(downloader, "REQUEST_SIZE", 6)
    partial = tmp_path / "model.part"
    partial.write_bytes(b"abc")
    requests = []
    transport = transport_for(b"abcdefghijklmnop", requests, interrupted="bytes=3-8")
    downloader.ranged_download("test", "https://example.org/model", partial, 1,
                               transport=transport, retry_delay=lambda _: None)
    assert partial.read_bytes() == b"abcdefghijklmnop"
    assert "bytes=6-8" in requests
    assert requests.count("bytes=3-8") == 1


def test_reuses_existing_fragments_after_partial_assembly(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "CHUNK_SIZE", 8)
    monkeypatch.setattr(downloader, "REQUEST_SIZE", 4)
    partial = tmp_path / "model.part"
    partial.write_bytes(b"abcdef")
    parts = partial.with_suffix(".part.ranges")
    parts.mkdir()
    (parts / "3-10.part").write_bytes(b"defghijk")
    requests = []
    downloader.ranged_download("test", "https://example.org/model", partial, 1,
                               transport=transport_for(b"abcdefghijklmnop", requests),
                               retry_delay=lambda _: None)
    assert partial.read_bytes() == b"abcdefghijklmnop"
    assert requests == ["bytes=0-0", "bytes=11-14", "bytes=15-15"]


def test_http_denial_is_not_retried(tmp_path):
    requests = []
    with pytest.raises(httpx.HTTPStatusError):
        downloader.ranged_download("test", "https://example.org/model", tmp_path / "model.part", 1,
                                   transport=transport_for(b"abcdef", requests, forbidden=True),
                                   retry_delay=lambda _: pytest.fail("HTTP denial was retried"))
    assert len(requests) == 2


def test_retries_are_bounded_and_partial_data_survives(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "MAX_TRANSPORT_ATTEMPTS", 2)
    requests = []
    def handle(request):
        requests.append(request.headers["Range"])
        if len(requests) == 1:
            return httpx.Response(206, headers={"Content-Range": "bytes 0-0/9"}, content=b"a")
        if len(requests) == 2:
            return httpx.Response(206, headers={"Content-Range": "bytes 0-8/9"}, stream=InterruptedStream())
        raise httpx.ReadTimeout("connection timed out")
    partial = tmp_path / "model.part"
    with pytest.raises(RuntimeError, match="after 2 attempts"):
        downloader.ranged_download("test", "https://example.org/model", partial, 1,
                                   transport=httpx.MockTransport(handle), retry_delay=lambda _: None)
    assert requests[-1] == "bytes=3-8"
    assert (partial.with_suffix(".part.ranges") / "0-8.part").read_bytes() == b"def"
