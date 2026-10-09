"""Download pinned official runtime/model assets with SHA-256 verification."""
import argparse
import hashlib
import re
import time
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
ASSETS = {
    "runtime": ("https://github.com/ollama/ollama/releases/download/v0.40.1/ollama-windows-amd64.zip",
                BACKEND / ".runtime" / "ollama-windows-amd64-v0.40.1.zip",
                "b394d14436d38032f23190e3f14eb2c6dad5ebbe4e192414f74c8fdca01703ab"),
    "finance_chat": ("https://huggingface.co/TheBloke/finance-chat-GGUF/resolve/main/finance-chat.Q4_K_M.gguf",
                     BACKEND / "models" / "finance-chat.Q4_K_M.gguf",
                     "f6808c32835587187d464fe6a056ab9bbe6dea019a53d1315f244de5f16bb372"),
    "fin_r1": ("https://huggingface.co/bartowski/SUFE-AIFLM-Lab_Fin-R1-GGUF/resolve/main/SUFE-AIFLM-Lab_Fin-R1-Q4_K_M.gguf",
               BACKEND / "models" / "SUFE-AIFLM-Lab_Fin-R1-Q4_K_M.gguf",
               "d50f16c5149b4dc103c68e249a136ab7c82f7569a7df707a2d6150bff5994c33"),
}


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


CHUNK_SIZE = 64 * 1024 * 1024
REQUEST_SIZE = 8 * 1024 * 1024
MAX_TRANSPORT_ATTEMPTS = 4


def _range_plan(parts, prefix, total):
    """Keep existing interval names, including a partially assembled interval."""
    existing = []
    for path in parts.iterdir():
        match = re.fullmatch(r"(\d+)-(\d+)\.part", path.name)
        if not match or not path.is_file():
            continue
        start, end = map(int, match.groups())
        if start > end or end >= total or path.stat().st_size > end - start + 1:
            raise RuntimeError("Range file has invalid bounds or size")
        existing.append((start, end))
    existing.sort()
    intervals = []
    cursor = prefix
    previous_end = -1
    for start, end in existing:
        if start <= previous_end:
            raise RuntimeError("Existing range files overlap; preserved for inspection")
        previous_end = end
        if end < prefix:
            continue
        while cursor < start:
            gap_end = min(cursor + CHUNK_SIZE - 1, start - 1)
            intervals.append((cursor, gap_end))
            cursor = gap_end + 1
        intervals.append((start, end))
        cursor = end + 1
    while cursor < total:
        end = min(cursor + CHUNK_SIZE - 1, total - 1)
        intervals.append((cursor, end))
        cursor = end + 1
    return intervals


def ranged_download(name, url, partial, workers, *, transport=None, retry_delay=time.sleep):
    """Resume exact byte offsets; retry transport failures, never HTTP denials.

    Fragment files remain 64 MiB, but each request is at most 8 MiB so a
    connection need not stay open for a whole fragment on slow connections.
    The final assembled file is still checked against the pinned SHA-256.
    """
    timeout = httpx.Timeout(90, connect=30)
    client_options = dict(follow_redirects=True, timeout=timeout, transport=transport)
    with httpx.Client(**client_options) as probe:
        with probe.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
            response.raise_for_status()
            if response.status_code != 206 or not response.headers.get("Content-Range", "").startswith("bytes 0-0/"):
                raise RuntimeError("Server does not support verified byte ranges")
            total = int(response.headers["Content-Range"].split("/")[-1])
    prefix = partial.stat().st_size if partial.exists() else 0
    if prefix > total:
        raise RuntimeError("Partial download exceeds source length")
    parts = partial.with_suffix(partial.suffix + ".ranges")
    parts.mkdir(exist_ok=True)
    intervals = _range_plan(parts, prefix, total)
    lock = threading.Lock()
    completed = prefix
    last_tick = time.monotonic()

    def fetch(interval):
        nonlocal completed, last_tick
        start, end = interval
        path = parts / f"{start}-{end}.part"
        available = path.stat().st_size if path.exists() else 0
        if available > end - start + 1:
            raise RuntimeError("Range file has invalid size")
        with lock:
            completed += max(0, available - max(0, prefix - start))
        if available == end - start + 1:
            return path
        with httpx.Client(**client_options) as client:
            while available < end - start + 1:
                request_end = min(start + available + REQUEST_SIZE - 1, end)
                for attempt in range(MAX_TRANSPORT_ATTEMPTS):
                    begin = start + path.stat().st_size if path.exists() else start
                    try:
                        headers = {"Range": f"bytes={begin}-{request_end}", "Accept-Encoding": "identity"}
                        with client.stream("GET", url, headers=headers) as response:
                            response.raise_for_status()
                            expected_range = f"bytes {begin}-{request_end}/{total}"
                            if response.status_code != 206 or response.headers.get("Content-Range") != expected_range:
                                raise RuntimeError("Server returned a mismatched byte range")
                            with path.open("ab") as stream:
                                received = 0
                                for block in response.iter_bytes():
                                    if received + len(block) > request_end - begin + 1:
                                        raise RuntimeError("Server returned more bytes than the requested range")
                                    stream.write(block)
                                    received += len(block)
                                    with lock:
                                        completed += len(block)
                                        if time.monotonic() - last_tick >= 30:
                                            print(f"{name}: {completed / 1024**3:.2f} GiB ({completed * 100 / total:.0f}%)", flush=True)
                                            last_tick = time.monotonic()
                        if path.stat().st_size != request_end - start + 1:
                            raise httpx.RemoteProtocolError("Server returned an incomplete byte range")
                        break
                    except httpx.TransportError as error:
                        if path.exists() and path.stat().st_size == request_end - start + 1:
                            break
                        if attempt + 1 == MAX_TRANSPORT_ATTEMPTS:
                            raise RuntimeError(
                                f"Transport failed after {MAX_TRANSPORT_ATTEMPTS} attempts; "
                                f"saved bytes remain resumable in {path.name}"
                            ) from error
                        print(f"{name}: connection interrupted; resuming {path.name} "
                              f"(attempt {attempt + 2}/{MAX_TRANSPORT_ATTEMPTS})", flush=True)
                        retry_delay(min(2 ** attempt, 8))
                available = path.stat().st_size
        if path.stat().st_size != end - start + 1:
            raise RuntimeError("Server returned an incomplete byte range")
        return path

    with ThreadPoolExecutor(max_workers=workers) as executor:
        finished = list(executor.map(fetch, intervals))
    with partial.open("ab") as stream:
        for path in finished:
            match = re.fullmatch(r"(\d+)-(\d+)\.part", path.name)
            start = int(match.group(1))
            # If assembly was interrupted, its valid prefix already contains
            # some or all of this fragment. Never append those bytes twice.
            skip = max(0, stream.tell() - start)
            with path.open("rb") as segment:
                segment.seek(skip)
                for block in iter(lambda: segment.read(4 * 1024 * 1024), b""):
                    stream.write(block)
            stream.flush()
    if partial.stat().st_size != total:
        raise RuntimeError("Assembled download has incorrect size")


def download(name, connections=1):
    url, target, expected = ASSETS[name]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if checksum(target) != expected:
            raise RuntimeError("Existing asset failed checksum verification; preserved for inspection.")
        print(f"{name}: verified existing asset", flush=True)
    else:
        partial = target.with_suffix(target.suffix + ".part")
        ranged_download(name, url, partial, connections)
        if checksum(partial) != expected:
            raise RuntimeError("Downloaded asset failed SHA-256 verification; partial file preserved.")
        partial.replace(target)
        print(f"{name}: download and SHA-256 verification complete", flush=True)
        # Remove only this downloader's now-redundant range files after the
        # assembled asset passes its pinned hash; never recursively delete.
        range_directory = partial.with_suffix(partial.suffix + ".ranges").resolve()
        if range_directory.is_relative_to((BACKEND / "models").resolve()) and range_directory.is_dir():
            for fragment in range_directory.iterdir():
                if fragment.is_file() and fragment.name.endswith(".part") and all(
                    value.isdigit() for value in fragment.name.removesuffix(".part").split("-")
                ):
                    fragment.unlink()
            if not any(range_directory.iterdir()):
                range_directory.rmdir()
    if name == "runtime":
        destination = (BACKEND / ".runtime" / "ollama-v0.40.1").resolve()
        with zipfile.ZipFile(target) as archive:
            for member in archive.infolist():
                member_path = (destination / member.filename).resolve()
                if not member_path.is_relative_to(destination):
                    raise RuntimeError("Runtime archive contained an unsafe path")
            archive.extractall(destination)
        print(f"runtime: extracted to {destination}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("asset", choices=ASSETS)
    parser.add_argument("--connections", type=int, default=1, choices=range(1, 9))
    arguments = parser.parse_args()
    download(arguments.asset, arguments.connections)
