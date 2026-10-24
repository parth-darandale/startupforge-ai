"""Install and operate the genuine AutoPresent LoRA with a quantized Llama base.

Usage: .venv\\Scripts\\python.exe scripts\\setup_autopresent.py setup --start
The conversion environment, vendor tools, logs and model weights stay ignored.
No model merging, gated full-weight downloads or generic model substitutions.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
import zipfile

BACKEND = Path(__file__).resolve().parents[1]
RUNTIME = BACKEND / ".runtime" / "autopresent"
MODELS = BACKEND / "models" / "autopresent"
MANIFEST = MODELS / "manifest.json"
SERVICE = RUNTIME / "service.json"
WORKER_STATE = BACKEND / ".runtime" / "autopresent-service.json"
BASE = MODELS / "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
ADAPTER_DIR = MODELS / "adapter"
ADAPTER = MODELS / "autopresent-detailed-f16.gguf"
VENDOR = RUNTIME / "llama.cpp-b11838"
SOURCE = RUNTIME / "llama.cpp-source"
CONVERT_PYTHON = RUNTIME / "conversion-venv" / "Scripts" / "python.exe"
ALIAS = "startupforge-autopresent"
URL = "http://127.0.0.1:8091"
LLAMA_COMMIT = "4231b23dc2c46661d01157aac5245bdcf03ce4b1"
ADAPTER_COMMIT = "a4dc4d22e57671c4edbddae82f0355d1e07b2108"
BASE_COMMIT = "sha256:667b0c1932bc6ffc593ed1d03f895bf2dc8dc6df21db3042284a6f4416b06a29"
CONFIG_COMMIT = "4699cc75b550f9c6f3173fb80f4703b62d946aa5"
HF = "https://huggingface.co"
ASSETS = {
    "base": (f"https://registry.ollama.ai/v2/library/llama3.1/blobs/{BASE_COMMIT}",
             BASE, "667b0c1932bc6ffc593ed1d03f895bf2dc8dc6df21db3042284a6f4416b06a29", 4920738944),
    "adapter": (f"{HF}/JiaxinGe/llama-3.1-8b-autopresent_detailed_instruction/resolve/{ADAPTER_COMMIT}/adapter_model.safetensors",
                ADAPTER_DIR / "adapter_model.safetensors", "18ac6b92bc96b2ed6ab126814d4ee672b0869bb81b0efc1d4e36707148b3db09", 1342238560),
    "adapter_config": (f"{HF}/JiaxinGe/llama-3.1-8b-autopresent_detailed_instruction/resolve/{ADAPTER_COMMIT}/adapter_config.json",
                       ADAPTER_DIR / "adapter_config.json", "12b44e49c0d68b38b18d08b14fadd602e038e39dad64d10a9acfe2329113c9b8", 733),
    "base_config": (f"{HF}/unsloth/Llama-3.1-8B-Instruct/resolve/{CONFIG_COMMIT}/config.json",
                    MODELS / "base-config" / "config.json", "ad98082c6bc4ea7078915219241814762be259de16d6ca5f170ba02820b3928b", 896),
    "llama_binary": ("https://github.com/ggml-org/llama.cpp/releases/download/b11838/llama-b11838-bin-win-cuda-12.4-x64.zip",
                     RUNTIME / "llama-b11838-bin-win-cuda-12.4-x64.zip", "84589aea6b7b55e08cb9e2a6aab43b41cbb3a067e8274019bef93788d76027d6", 272332183),
}


def bootstrap_setup(arguments: list[str]) -> int | None:
    """Download and convert with private dependencies, even on a clean app venv.

    ``start``, ``stop`` and ``status`` remain standard-library operations. Only
    setup creates this environment and re-enters the script. Check the active
    prefix rather than executable names so the re-entry cannot recurse.
    """
    environment_root = CONVERT_PYTHON.parent.parent.resolve()
    if not CONVERT_PYTHON.is_file():
        environment_root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", str(environment_root)], check=True)
    requirements = BACKEND / "requirements-autopresent-setup.txt"
    version_check = (
        "import importlib.metadata as metadata, pathlib, sys\n"
        "for line in pathlib.Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():\n"
        " line = line.strip()\n"
        " if not line or line.startswith('#'): continue\n"
        " package, version = line.split('==', 1)\n"
        " try: installed = metadata.version(package)\n"
        " except metadata.PackageNotFoundError: sys.exit(1)\n"
        " if installed != version: sys.exit(1)\n"
    )
    ready = subprocess.run([str(CONVERT_PYTHON), "-c", version_check, str(requirements)],
                           capture_output=True, check=False)
    if ready.returncode:
        print("Installing AutoPresent download dependencies in its private environment.", flush=True)
        subprocess.run([str(CONVERT_PYTHON), "-m", "pip", "install", "--no-cache-dir",
                        "-r", str(requirements)], check=True)
    if Path(sys.prefix).resolve() == environment_root:
        return None
    print("Continuing AutoPresent setup in its private environment.", flush=True)
    return subprocess.run([str(CONVERT_PYTHON), str(Path(__file__).resolve()), *arguments], check=False).returncode


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(name: str, url: str, target: Path, expected: str | None = None,
             size: int | None = None) -> Path:
    """Resume interrupted downloads; verify publisher SHA-256 before using assets."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (size is None or target.stat().st_size == size):
        if expected is None or checksum(target) == expected:
            print(f"{name}: already verified", flush=True)
            return target
    part = target.with_name(target.name + ".part")
    mirror = os.environ.get("AUTOPRESENT_HF_MIRROR", "").rstrip("/")
    if name == "adapter" and mirror:
        if mirror != "https://hf-mirror.com":
            raise RuntimeError("Supported optional mirror: https://hf-mirror.com")
        return ranged_download(name, mirror + url.removeprefix(HF), target, expected, size)
    if name == "adapter":
        return hub_download(name, url, target, expected, size)
    if size and size > 64 * 1024 * 1024:
        return ranged_download(name, url, target, expected, size)
    for attempt in range(5):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "StartupForge-AutoPresent/1.0"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request_url = url + ("&" if "?" in url else "?") + f"download=true&startupforge_retry={attempt}"
        try:
            with urllib.request.urlopen(urllib.request.Request(request_url, headers=headers), timeout=90) as response:
                append = offset > 0 and response.status == 206
                if not append:
                    offset = 0
                total = size or (int(response.headers.get("Content-Length", "0")) + offset)
                previous = offset * 10 // total if total else 0
                with part.open("ab" if append else "wb") as stream:
                    count = offset
                    while chunk := response.read(4 * 1024 * 1024):
                        stream.write(chunk)
                        count += len(chunk)
                        bucket = count * 10 // total if total else 0
                        if bucket > previous:
                            print(f"{name}: {min(bucket * 10, 100)}%", flush=True)
                            previous = bucket
            if size is not None and part.stat().st_size != size:
                raise RuntimeError(f"{name}: incomplete download ({part.stat().st_size}/{size})")
            if expected and checksum(part) != expected:
                part.unlink()
                raise RuntimeError(f"{name}: publisher SHA-256 mismatch")
            part.replace(target)
            print(f"{name}: verified", flush=True)
            return target
        except (OSError, urllib.error.URLError, RuntimeError) as error:
            print(f"{name}: retry {attempt + 1}/5: {error}", flush=True)
            if attempt == 4:
                raise
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"{name}: download failed")


def hub_download(name: str, url: str, target: Path, expected: str | None, size: int | None) -> Path:
    """Use Hugging Face's CAS/Xet client instead of slow LFS HTTP streams."""
    os.environ["HF_XET_HIGH_PERFORMANCE"] = "1"
    os.environ["HF_XET_CHUNK_CACHE_SIZE_BYTES"] = "0"
    os.environ["HF_XET_SHARD_CACHE_SIZE_LIMIT"] = "0"
    os.environ["HF_XET_NUM_CONCURRENT_RANGE_GETS"] = "16"
    os.environ["HF_XET_CACHE"] = str(RUNTIME / "xet")
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    from huggingface_hub import hf_hub_download
    from tqdm.auto import tqdm
    class DecileProgress(tqdm):
        def __init__(self, *args, **kwargs):
            self.last_decile = -1
            super().__init__(*args, **{**kwargs, "disable": True})
        def update(self, n=1):
            # tqdm's disabled mode does not advance n, so track it explicitly.
            self.n += n
            bucket = int(self.n * 10 // self.total) if self.total else 0
            if bucket > self.last_decile:
                self.last_decile = bucket
                print(f"{name}: {min(bucket * 10, 100)}%", flush=True)
    relative = url.removeprefix(HF + "/")
    repo, rest = relative.split("/resolve/", 1)
    revision, filename = rest.split("/", 1)
    result = Path(hf_hub_download(repo_id=repo, filename=filename, revision=revision,
                                local_dir=str(target.parent), token=False, tqdm_class=DecileProgress))
    if size is not None and result.stat().st_size != size:
        raise RuntimeError(f"{name}: unexpected downloaded size")
    if expected and checksum(result) != expected:
        raise RuntimeError(f"{name}: publisher SHA-256 mismatch")
    if result != target:
        result.replace(target)
    print(f"{name}: verified", flush=True)
    return target


def ranged_download(name: str, url: str, target: Path, expected: str | None, size: int) -> Path:
    """Download verified byte ranges straight into one file, with a resume map.

    Concurrent byte requests avoid full-file proxy stalls and do not
    retain a second copy of the multi-gigabyte base during assembly.
    """
    import httpx
    partial = target.with_name(target.name + ".part")
    resume = target.with_name(target.name + ".ranges.json")
    chunk_size = 8 * 1024 * 1024 if name == "base" else 1024 * 1024
    saved = json.loads(resume.read_text()) if resume.exists() and partial.exists() else {}
    completed = set(saved.get("completed", []))
    if saved and (saved.get("size") != size or saved.get("sha256") != expected
                  or saved.get("chunk_size", 1024 * 1024) != chunk_size):
        raise RuntimeError(f"{name}: resume map differs from pinned source")
    with partial.open("r+b" if partial.exists() else "w+b") as output:
        output.truncate(size)
    lock = threading.Lock()
    progress = {"bucket": -1}
    count_chunks = (size + chunk_size - 1) // chunk_size
    client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(45, connect=20),
                          limits=httpx.Limits(max_connections=12, max_keepalive_connections=12))
    endpoint = {"url": url}
    endpoint_lock = threading.Lock()

    def resolve_endpoint() -> str:
        # Signed object URLs permit byte ranges. Reuse the CDN connection rather
        # than repeating the hub/registry redirect chain for every small chunk.
        with endpoint_lock:
            response = client.head(url, headers={"Accept-Encoding": "identity"})
            response.raise_for_status()
            endpoint["url"] = str(response.url)
            return endpoint["url"]

    resolve_endpoint()

    def fetch(index: int) -> None:
        if index in completed:
            return
        begin, end = index * chunk_size, min((index + 1) * chunk_size, size) - 1
        for attempt in range(5):
            try:
                response = client.get(endpoint["url"], headers={"Range": f"bytes={begin}-{end}", "Accept-Encoding": "identity"})
                if response.status_code == 403:
                    resolve_endpoint()
                    raise RuntimeError("Refreshed expired download URL")
                response.raise_for_status()
                if response.status_code != 206 or response.headers.get("Content-Range") != f"bytes {begin}-{end}/{size}":
                    raise RuntimeError("Server returned a mismatched byte range")
                if len(response.content) != end - begin + 1:
                    raise RuntimeError("Server returned an incomplete byte range")
                with lock:
                    with partial.open("r+b") as output:
                        output.seek(begin)
                        output.write(response.content)
                        output.flush()
                    completed.add(index)
                    state = {"size": size, "sha256": expected, "chunk_size": chunk_size, "completed": sorted(completed)}
                    resume.write_text(json.dumps(state), encoding="utf-8")
                    bucket = len(completed) * 10 // count_chunks
                    if bucket > progress["bucket"]:
                        progress["bucket"] = bucket
                        print(f"{name}: {bucket * 10}%", flush=True)
                return
            except (OSError, httpx.HTTPError, RuntimeError) as error:
                if attempt == 4:
                    raise RuntimeError(f"{name} chunk {index} failed after retries") from error
                time.sleep(min(2 ** attempt, 8))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for _ in pool.map(fetch, range(count_chunks)):
                pass
    finally:
        client.close()
    if expected and checksum(partial) != expected:
        raise RuntimeError(f"{name}: publisher SHA-256 mismatch; preserved for inspection")
    partial.replace(target)
    resume.unlink(missing_ok=True)
    print(f"{name}: verified", flush=True)
    return target


def safe_extract(archive: Path, destination: Path, source_only: bool = False) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            parts = Path(item.filename).parts
            relative = Path(*parts[1:]) if source_only else Path(item.filename)
            if not relative.parts:
                continue
            if source_only and relative.parts[0] not in {"convert_lora_to_gguf.py", "conversion", "gguf-py"}:
                continue
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError("Unsafe path in vendor archive")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def fetch_converter_sources() -> dict:
    """Fetch the pinned converter's small Llama-only source dependency closure.

    GitHub's full source archive includes unrelated large assets and can stall.
    The tree API pins each required file to a Git blob ID; downloaded bytes must
    match that ID before becoming usable source. SHA-256 is retained as well.
    """
    repository = "ggml-org/llama.cpp"
    api = f"https://api.github.com/repos/{repository}"
    manifest_path = SOURCE / "source-manifest.json"
    required = {"convert_lora_to_gguf.py", "conversion/__init__.py", "conversion/base.py",
                "conversion/llama.py", "gguf-py/pyproject.toml", "gguf-py/README.md",
                "gguf-py/LICENSE", "LICENSE", "gguf-py/gguf/__init__.py"}
    SOURCE.mkdir(parents=True, exist_ok=True)

    def blob_hash(data: bytes) -> str:
        return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()

    def target_for(relative: str) -> Path:
        target = (SOURCE / relative).resolve()
        if not target.is_relative_to(SOURCE.resolve()) or "\\" in relative:
            raise RuntimeError("Unsafe path in converter source tree")
        return target

    def verified_file(item: dict) -> bool:
        target = target_for(item["path"])
        return (target.is_file() and target.stat().st_size == item["size"]
                and blob_hash(target.read_bytes()) == item["git_blob_sha1"])

    try:
        cached = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (cached.get("revision") == LLAMA_COMMIT
                and required.issubset({item["path"] for item in cached["files"]})
                and all(verified_file(item) for item in cached["files"])):
            print("llama_converter_source: all pinned source files already verified", flush=True)
            return cached
    except (OSError, ValueError, KeyError, TypeError):
        pass

    def request_bytes(url: str) -> bytes:
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={
                    "User-Agent": "StartupForge-AutoPresent/1.0", "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(request, timeout=25) as response:
                    return response.read(4 * 1024 * 1024 + 1)
            except (OSError, urllib.error.URLError):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        raise RuntimeError("Converter source request failed")

    tree_url = f"{api}/git/trees/{LLAMA_COMMIT}?recursive=1"
    tree = json.loads(request_bytes(tree_url))
    if tree.get("truncated") or not isinstance(tree.get("tree"), list):
        raise RuntimeError("GitHub returned an incomplete pinned converter source tree")
    files = []
    for entry in tree["tree"]:
        relative = entry.get("path", "")
        if entry.get("type") != "blob":
            continue
        wanted = (relative in required or relative.startswith("gguf-py/gguf/")
                  and (relative.endswith(".py") or relative.endswith("/py.typed")))
        if not wanted:
            continue
        target_for(relative)
        files.append({"path": relative, "size": entry["size"], "git_blob_sha1": entry["sha"],
                      "url": f"https://raw.githubusercontent.com/{repository}/{LLAMA_COMMIT}/{relative}"})
    if not required.issubset({item["path"] for item in files}):
        raise RuntimeError("Pinned source tree is missing converter dependencies")

    def fetch(item: dict) -> dict:
        target = target_for(item["path"])
        if not verified_file(item):
            # The blob endpoint returns small base64 JSON bodies reliably even
            # where archive/raw downloads stall. Raw remains a rate-limit fallback.
            try:
                blob = json.loads(request_bytes(f"{api}/git/blobs/{item['git_blob_sha1']}"))
                if blob.get("encoding") != "base64":
                    raise ValueError("Unexpected Git blob encoding")
                data = base64.b64decode(blob["content"])
            except (OSError, ValueError, KeyError):
                data = request_bytes(item["url"])
            if len(data) != item["size"] or blob_hash(data) != item["git_blob_sha1"]:
                raise RuntimeError("Pinned converter source checksum mismatch: " + item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".part")
            temporary.write_bytes(data)
            temporary.replace(target)
        return {**item, "sha256": checksum(target)}

    print(f"llama_converter_source: verifying {len(files)} small pinned files", flush=True)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for item in pool.map(fetch, files):
            results.append(item)
            if len(results) % 5 == 0 or len(results) == len(files):
                print(f"llama_converter_source: {len(results)}/{len(files)} verified", flush=True)
    manifest = {"repository": repository, "revision": LLAMA_COMMIT, "tree_url": tree_url,
                "tree_git_sha1": tree["sha"], "files": results}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def setup() -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    print("Installing real AutoPresent detailed LoRA + matching Llama 3.1 base.", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(download, name, *asset) for name, asset in ASSETS.items()]
        for job in jobs:
            job.result()
    if not (VENDOR / "llama-server.exe").exists():
        safe_extract(ASSETS["llama_binary"][1], VENDOR)
    converter_sources = fetch_converter_sources()
    if not CONVERT_PYTHON.exists():
        subprocess.run([sys.executable, "-m", "venv", str(CONVERT_PYTHON.parent.parent)], check=True)
    marker = RUNTIME / "conversion-dependencies.ready"
    if not marker.exists():
        subprocess.run([str(CONVERT_PYTHON), "-m", "pip", "install", "--no-cache-dir",
                        "--extra-index-url", "https://download.pytorch.org/whl/cpu", "torch==2.6.0+cpu"], check=True)
        subprocess.run([str(CONVERT_PYTHON), "-m", "pip", "install", "--no-cache-dir", "-r",
                        str(BACKEND / "requirements-autopresent.txt"), str(SOURCE / "gguf-py")], check=True)
        marker.write_text("torch 2.6.0 cpu; pinned conversion-only requirements\n", encoding="utf-8")
    if not ADAPTER.exists():
        temporary = ADAPTER.with_name(ADAPTER.name + ".part")
        subprocess.run([str(CONVERT_PYTHON), str(SOURCE / "convert_lora_to_gguf.py"),
                        "--base", str(MODELS / "base-config"), "--outtype", "f16",
                        "--outfile", str(temporary), str(ADAPTER_DIR)], check=True)
        temporary.replace(ADAPTER)
    manifest = {
        "model": ALIAS,
        "adapter_repository": "JiaxinGe/llama-3.1-8b-autopresent_detailed_instruction",
        "adapter_revision": ADAPTER_COMMIT,
        "base_repository": "registry.ollama.ai/library/llama3.1:8b-instruct-q4_K_M",
        "base_revision": BASE_COMMIT,
        "base_original": "meta-llama/Llama-3.1-8B-Instruct",
        "converter_revision": LLAMA_COMMIT,
        "conversion": "official llama.cpp LoRA-to-GGUF, f16, no merging",
        "assets": {name: {"url": asset[0], "path": str(asset[1].relative_to(BACKEND)),
                           "sha256": asset[2], "size": asset[3]} for name, asset in ASSETS.items()},
        "converted_adapter": {"path": str(ADAPTER.relative_to(BACKEND)), "sha256": checksum(ADAPTER), "size": ADAPTER.stat().st_size},
        "converter_sources": converter_sources,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"AutoPresent assets and conversion ready. Manifest: {MANIFEST}", flush=True)


def health() -> bool:
    try:
        with urllib.request.urlopen(URL + "/v1/models", timeout=3) as response:
            data = json.load(response)
        return any(model.get("id") == ALIAS for model in data.get("data", []))
    except (OSError, ValueError):
        return False


def start_server(gpu_layers: int = 30) -> dict:
    if health():
        print("AutoPresent already available at " + URL, flush=True)
        return json.loads(SERVICE.read_text()) if SERVICE.exists() else {"url": URL, "owned": False}
    if not MANIFEST.exists() or not ADAPTER.exists() or not BASE.exists():
        raise RuntimeError("AutoPresent assets missing. Run setup first.")
    executable = VENDOR / "llama-server.exe"
    cuda_dirs = sorted((BACKEND / ".runtime").glob("ollama-*/lib/ollama/cuda_v12"), reverse=True)
    environment = os.environ.copy()
    if cuda_dirs:
        # Reuse only CUDA runtime DLLs; llama.cpp's own ggml-cuda.dll stays first.
        environment["PATH"] = os.pathsep.join([str(VENDOR), str(cuda_dirs[0]), environment.get("PATH", "")])
    command = [str(executable), "--model", str(BASE), "--lora", str(ADAPTER),
               "--alias", ALIAS, "--host", "127.0.0.1", "--port", "8091",
               "--ctx-size", "8192", "--parallel", "1", "--n-gpu-layers", str(gpu_layers),
               "--flash-attn", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
               "--batch-size", "256", "--ubatch-size", "128", "--no-webui"]
    output = BACKEND / ".runtime" / "autopresent-stdout.log"
    error = BACKEND / ".runtime" / "autopresent-stderr.log"
    from start_local_services import spawn_background
    service_pid = spawn_background(command, "autopresent", environment)
    worker_state = json.loads(WORKER_STATE.read_text())
    metadata = {**worker_state, "service_pid": service_pid, "executable": str(executable), "command": command,
                "url": URL, "model": ALIAS, "status": "starting", "started_at": time.time(),
                "stdout_log": str(output), "stderr_log": str(error), "owned": True}
    SERVICE.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = json.loads(WORKER_STATE.read_text())
        if state.get("status") in {"failed", "exited"}:
            metadata["status"] = "failed"
            SERVICE.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            raise RuntimeError(f"AutoPresent exited {state.get('exit_code')}; see {error}")
        if health():
            metadata["status"] = "running"
            SERVICE.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            print(f"AutoPresent running (PID {service_pid}) at {URL}", flush=True)
            return metadata
        time.sleep(2)
    raise RuntimeError(f"AutoPresent still starting; check {error}")


def stop_server() -> None:
    if not WORKER_STATE.exists():
        print("No owned AutoPresent service recorded.")
        return
    metadata = json.loads(WORKER_STATE.read_text())
    pid = int(metadata["service_pid"])
    if os.name != "nt":
        raise RuntimeError("This installer manages Windows processes only.")
    # Validate executable + command before terminating a possibly recycled PID.
    query = f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | Select-Object ExecutablePath,CommandLine,ParentProcessId | ConvertTo-Json -Compress"
    result = subprocess.run(["powershell", "-NoProfile", "-Command", query], capture_output=True,
                            text=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15, check=True)
    if result.stdout.strip():
        process = json.loads(result.stdout)
        expected = str(VENDOR / "llama-server.exe").lower()
        command_line = (process.get("CommandLine") or "").lower()
        if ((process.get("ExecutablePath") or "").lower() != expected
                or process.get("ParentProcessId") != metadata.get("worker_pid")
                or any(value.lower() not in command_line for value in (ALIAS, str(BASE), str(ADAPTER)))):
            raise RuntimeError("Recorded PID is not this project's AutoPresent service; refusing to stop it.")
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=True, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    metadata["status"] = "stopped"
    if SERVICE.exists():
        SERVICE.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print("AutoPresent stopped.", flush=True)


def status() -> None:
    print(json.dumps({"assets_ready": MANIFEST.exists() and ADAPTER.exists() and BASE.exists(),
                      "healthy": health(), "url": URL, "model": ALIAS,
                      "manifest": str(MANIFEST), "service": json.loads(SERVICE.read_text()) if SERVICE.exists() else None}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["setup", "start", "stop", "status"])
    parser.add_argument("--start-only", action="store_true", help="start installed assets without network downloads")
    parser.add_argument("--stop", action="store_true", help="stop the owned local model server")
    parser.add_argument("--start", action="store_true", help="start after setup")
    parser.add_argument("--gpu-layers", type=int, default=30)
    args = parser.parse_args()
    if args.start_only:
        args.command = "start"
    elif args.stop:
        args.command = "stop"
    elif args.command is None:
        parser.error("Pass setup, start, stop or status.")
    if args.command == "setup":
        bootstrap_result = bootstrap_setup(sys.argv[1:])
        if bootstrap_result is not None:
            raise SystemExit(bootstrap_result)
        setup()
        if args.start:
            start_server(args.gpu_layers)
    elif args.command == "start":
        start_server(args.gpu_layers)
    elif args.command == "stop":
        stop_server()
    else:
        status()
