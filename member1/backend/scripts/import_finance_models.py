"""Import verified GGUFs into this workspace's portable Ollama cache."""
import argparse
import json
import os
from pathlib import Path

import httpx

from download_model_assets import ASSETS, BACKEND, checksum

MODELS = {
    "finance_chat": ("startupforge-finance-chat", "[INST] <<SYS>>\n{{ .System }}\n<</SYS>>\n\n{{ .Prompt }} [/INST]"),
    "fin_r1": ("startupforge-fin-r1", "{{- range .Messages }}<|im_start|>{{ .Role }}\n{{ .Content }}<|im_end|>\n{{ end }}<|im_start|>assistant\n"),
}


def import_model(asset):
    url, source, expected = ASSETS[asset]
    if not source.is_file() or checksum(source) != expected:
        raise RuntimeError("The model must finish downloading and pass its pinned SHA-256 first.")
    model, template = MODELS[asset]
    cache = (BACKEND / "models" / "ollama").resolve()
    blobs = cache / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    blob = blobs / ("sha256-" + expected)
    if not blob.exists():
        # Both paths are on the workspace's NTFS volume. A hard link avoids
        # storing a second multi-gigabyte copy. Never overwrite an existing blob.
        os.link(source, blob)
    if blob.stat().st_size != source.stat().st_size or checksum(blob) != expected:
        raise RuntimeError("The Ollama cache blob does not match the verified model.")
    with httpx.Client(base_url="http://127.0.0.1:11434", timeout=180) as client:
        available = client.head("/api/blobs/sha256:" + expected)
        available.raise_for_status()
        response = client.post("/api/create", json={"model": model,
            "files": {source.name: "sha256:" + expected}, "template": template,
            "parameters": {"num_ctx": 4096}, "stream": False})
        response.raise_for_status()
        result = response.json()
        if result.get("status") != "success" or result.get("error"):
            raise RuntimeError("Ollama did not complete the GGUF import.")
        metadata = client.post("/api/show", json={"model": model})
        metadata.raise_for_status()
        shown = metadata.json()
    entry = {"asset": asset, "model": model, "source_url": url,
             "weight_sha256": expected, "size_bytes": source.stat().st_size,
             "template": shown.get("template"), "details": shown.get("details"),
             "capabilities": shown.get("capabilities"), "hard_linked": os.path.samefile(source, blob)}
    report = BACKEND.parent.parent / "integration" / "model_trials" / "model_imports.json"
    imports = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
    imports[asset] = entry
    report.write_text(json.dumps(imports, indent=2), encoding="utf-8")
    print(f"Imported {model}; verified weights share the cache blob without a second copy.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("asset", choices=MODELS)
    import_model(parser.parse_args().asset)
