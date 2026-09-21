#!/usr/bin/env python3
"""Mirror the Anima model stack from HuggingFace to R2.

FEAT_ANIMA in serverless_provision.sh reads these three files from the bucket,
same as BiRefNet, so that provisioning never depends on HuggingFace being up.
This is the one-off that puts them there.

Anima is not a checkpoint: it is a 2B DiT (finetune of Cosmos-Predict2-2B) with
a Qwen3-0.6B text encoder and the Qwen-Image VAE, and the three pieces go to
three different folders. Getting one of them wrong fails at load time with a
message about the *node*, not the file, so the keys here are the contract.

    python scripts/mirror_anima.py            # mirror what is missing
    python scripts/mirror_anima.py --force    # re-upload even if present
    python scripts/mirror_anima.py --model anima-turbo-v1.1

Streams straight from HF to R2 in 64 MB parts: nothing lands on local disk,
which matters because 5.6 GB is more free space than the worker has and about
as much as this repo's own output directory.
"""
from __future__ import annotations

import argparse
import sys

import config

HF = "https://huggingface.co/circlestone-labs/Anima/resolve/main/split_files"

# variant -> filename under split_files/diffusion_models/
VARIANTS = {
    "anima-aesthetic-v1.0": "anima-aesthetic-v1.0.safetensors",   # recommended
    "anima-aesthetic-v1.0b": "anima-aesthetic-v1.0b.safetensors",  # LoRAs only
    "anima-aesthetic-v1.1": "anima-aesthetic-v1.1.safetensors",
    "anima-base-v1.0": "anima-base-v1.0.safetensors",              # unrefined
    "anima-turbo-v1.0": "anima-turbo-v1.0.safetensors",
    "anima-turbo-v1.1": "anima-turbo-v1.1.safetensors",            # 8-12 steps
}

# The encoder and the VAE are shared by every variant.
SHARED = [
    ("text_encoders/qwen_3_06b_base.safetensors",
     "comfy-stack/models/text_encoders/qwen_3_06b_base.safetensors"),
    ("vae/qwen_image_vae.safetensors",
     "comfy-stack/models/vae/qwen_image_vae.safetensors"),
]

PART = 64 * 1024 * 1024


def _client():
    import boto3
    env = config.r2_credentials()
    return boto3.client("s3", endpoint_url=env["S3_ENDPOINT_URL"],
                        aws_access_key_id=env["S3_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["S3_SECRET_ACCESS_KEY"],
                        region_name=env["S3_REGION"]), env["S3_BUCKET_NAME"]


def _already(s3, bucket: str, key: str) -> int | None:
    """Size in the bucket, or None if the key is not there."""
    try:
        return s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except Exception:
        return None


def mirror(s3, bucket: str, src: str, key: str, force: bool) -> bool:
    import urllib.request

    url = f"{HF}/{src}"
    req = urllib.request.Request(url, headers={"User-Agent": "comfy-vast"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)

        have = _already(s3, bucket, key)
        if have is not None and not force:
            # Same size is good enough: HF serves immutable blobs by revision.
            if have == total:
                print(f"  = {key}  already there ({have / 2**30:.2f} GB)")
                return False
            print(f"  ! {key}  size differs ({have} vs {total}), re-uploading")

        print(f"  > {key}  ({total / 2**30:.2f} GB)")
        up = s3.create_multipart_upload(Bucket=bucket, Key=key)
        parts, n, done = [], 0, 0
        try:
            while True:
                chunk = resp.read(PART)
                if not chunk:
                    break
                n += 1
                out = s3.upload_part(Bucket=bucket, Key=key, PartNumber=n,
                                     UploadId=up["UploadId"], Body=chunk)
                parts.append({"ETag": out["ETag"], "PartNumber": n})
                done += len(chunk)
                pct = f"{done * 100 / total:.0f}%" if total else "?"
                print(f"    part {n:3d}  {done / 2**30:5.2f} GB  {pct}",
                      flush=True)
            s3.complete_multipart_upload(
                Bucket=bucket, Key=key, UploadId=up["UploadId"],
                MultipartUpload={"Parts": parts})
        except BaseException:
            # A half-finished multipart upload is billed until aborted.
            s3.abort_multipart_upload(Bucket=bucket, Key=key,
                                      UploadId=up["UploadId"])
            raise
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="anima-aesthetic-v1.0",
                   choices=sorted(VARIANTS), help="which Anima variant")
    p.add_argument("--force", action="store_true")
    a = p.parse_args()

    fname = VARIANTS[a.model]
    jobs = [(f"diffusion_models/{fname}",
             f"comfy-stack/models/diffusion_models/{fname}")] + SHARED

    s3, bucket = _client()
    print(f"r2://{bucket}  <-  {HF}")
    changed = sum(mirror(s3, bucket, src, key, a.force) for src, key in jobs)
    print(f"\n{changed} uploaded, {len(jobs) - changed} already present")
    if a.model != "anima-aesthetic-v1.0":
        print(f"NOTE: set ANIMA_UNET in ab_modelo.py to {fname}")
    print("Now set FEAT_ANIMA=true in scripts/serverless_provision.sh and run "
          "python scripts/renew_provisioning.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
