"""Download a pinned public GGUF with Hugging Face's official client."""
import argparse
import os
from pathlib import Path

from download_model_assets import ASSETS, BACKEND, checksum

REPOS = {"finance_chat": "TheBloke/finance-chat-GGUF",
         "fin_r1": "bartowski/SUFE-AIFLM-Lab_Fin-R1-GGUF"}


def download(asset):
    os.environ["HF_HOME"] = str(BACKEND / ".runtime" / "huggingface")
    os.environ["HF_XET_CACHE"] = str(BACKEND / ".runtime" / "xet")
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    from huggingface_hub import hf_hub_download
    _, target, expected = ASSETS[asset]
    print(f"Downloading {asset} with the official public Hub client.", flush=True)
    path = Path(hf_hub_download(repo_id=REPOS[asset], filename=target.name,
                              local_dir=str(target.parent), token=False))
    if checksum(path) != expected:
        raise RuntimeError("Downloaded GGUF does not match the pinned SHA-256.")
    print(f"{asset}: download and pinned SHA-256 verification complete", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("asset", choices=REPOS)
    download(parser.parse_args().asset)
