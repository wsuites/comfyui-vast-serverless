#!/usr/bin/env python3
"""
Uploads serverless_provision.sh to R2 and points the endpoint template at it.

Run after editing serverless_provision.sh:

    python scripts/renew_provisioning.py
    python scripts/renew_provisioning.py --update-workers   # also refreshes live workers

PROVISIONING_SCRIPT points to the bucket's Public Development URL
(R2_PUBLIC_BASE), which is permanent and does not expire. With --presigned a
7-day signed URL is generated instead, useful if public access is ever
disabled.

Credentials: S3_* environment variables or a .env file next to this script.
(Vast masks values in `vastai show env-vars`, they cannot be read from there.)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "serverless_provision.sh"
import config
# Bucket Public Development URL (R2 -> Settings -> Public Development URL).
# Permanent and without query string, unlike a presigned one.
BUCKET_KEY = config.var("R2_PROVISION_KEY")
R2_PUBLIC_BASE = config.var("R2_PUBLIC_BASE")
TEMPLATE_ID = config.entero("VAST_TEMPLATE_ID")
TEMPLATE_NAME = config.var("VAST_TEMPLATE_NAME")
WORKERGROUP_ID = config.entero("VAST_WORKERGROUP_ID")
ENDPOINT_ID = config.entero("VAST_ENDPOINT_ID")
IMAGE = config.var("VAST_IMAGE")
IMAGE_TAG = config.var("VAST_IMAGE_TAG")
HF_TOKEN = config.var("VAST_HF_TOKEN")
EXPIRES = 7 * 24 * 3600  # maximum SigV4 allows

ONSTART = r"""export SERVERLESS=true
export BACKEND=comfyui-json
export COMFYUI_API_BASE="http://localhost:18188"
export MODEL_LOG=/var/log/portal/comfyui.log;
# Some hosts (old Docker seccomp + hiveos kernels) deny the faccessat2 syscall,
# so bash's builtin permission tests ([ -r ], [ -x ]) always return false while
# [ -f ], cat and /usr/bin/test keep working. boot_default.sh gates every
# /etc/vast_boot.d script behind `[[ -f ]] && [[ -r ]]`, so on such a host it
# sources ZERO of them: no supervisord, no ComfyUI, no models - an empty shell
# that still bills by the hour. Measured on machine 148233 (kernel 6.6.0-hiveos).
# The -r test is redundant there ([[ -f ]] already ran), so drop it - but only
# on a host that is actually broken, so healthy hosts boot byte-identically.
# 25-first-boot.sh carries the very same line; one sed covers both. The two
# scripts that matter (65-supervisor-launch, 75-provisioning-manifest) use no
# permission tests at all, which is why patching just this line is enough.
: > /tmp/.acc_probe
[ -r /tmp/.acc_probe ] || sed -i 's/&& \[\[ -r "$script" \]\] //' \
    /opt/instance-tools/bin/boot_default.sh /etc/vast_boot.d/25-first-boot.sh
rm -f /tmp/.acc_probe
entrypoint.sh &
wget -O - "https://raw.githubusercontent.com/vast-ai/pyworker/main/start_server.sh" | bash"""

# Disk allocated per worker. Paid per ALLOCATED GB, not used, and 24/7.
# Measured with everything installed (models + Impact + UltimateSDUpscale +
# RMBG): the writable layer uses 9.4GB, so at 16GB there is ~6.6GB of slack.
# NOTE: `du -sx /` gives 22GB, but that includes the read-only layers of the
# docker image, which do NOT count against the quota. The right number is the
# one from `df /`. Also disk_space>=16 in the filters opens up many more
# cheap hosts than >=32.
DISK_SPACE = config.var("VAST_DISK_SPACE")

# Offer filters. Keys of the real cost:
#   storage_cost  -> $/GB/month. 0.0625 * 32GB = 2$/month as ceiling.
#                    (market median is 0.20, i.e. 6.4$/month at 32GB)
#   inet_down_cost-> $/GB. 0.005 = 5$/TB. A cold start pulls ~22GB
#                    (image + models), i.e. ~3 cents. Tightening this filter
#                    more cuts a huge chunk of the selection to save nothing.
#   dlperf        -> performance floor, so we do not end up on a 4060 Ti.
#   dph_total     -> MANDATORY ceiling. Without it, among the machines with
#                    cheap disk there are H100s at 4.26$/h and the autoscaler
#                    may pick them.
# No verified/reliability: they do not matter for this workload.
SEARCH_PARAMS = config.var("VAST_SEARCH_PARAMS")

# NOTE: serverless workergroups do NOT support interruptible instances.
# Tested 2026-08-15: with `--launch_args "--bid_price 0.15"` the created
# instance comes out with is_bid=False. On-demand only.


def vastai(*args: str) -> str:
    proc = subprocess.run(["vastai", *args], capture_output=True, text=True,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if proc.returncode != 0:
        sys.exit(f"`vastai {' '.join(args)}` failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-workers", action="store_true",
                    help="also force a rolling update of the live workers")
    ap.add_argument("--presigned", action="store_true",
                    help="use a 7-day signed URL instead of the public one")
    args = ap.parse_args()

    if not SCRIPT.is_file():
        sys.exit(f"{SCRIPT} not found")

    env = config.r2_credentials()
    import boto3

    # the script must have LF line endings or bash chokes
    data = SCRIPT.read_bytes().replace(b"\r\n", b"\n")
    SCRIPT.write_bytes(data)

    s3 = boto3.client("s3", endpoint_url=env["S3_ENDPOINT_URL"],
                      aws_access_key_id=env["S3_ACCESS_KEY_ID"],
                      aws_secret_access_key=env["S3_SECRET_ACCESS_KEY"],
                      region_name=env["S3_REGION"])
    bucket = env["S3_BUCKET_NAME"]

    s3.put_object(Bucket=bucket, Key=BUCKET_KEY, Body=data,
                  ContentType="text/x-shellscript")
    print(f"uploaded to r2://{bucket}/{BUCKET_KEY} ({len(data)} bytes)")

    if args.presigned:
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": BUCKET_KEY}, ExpiresIn=EXPIRES)
        print("presigned URL regenerated (expires in 7 days)")
    else:
        url = f"{R2_PUBLIC_BASE}/{BUCKET_KEY}"
        # check that public access is still on before breaking the template.
        # Cloudflare returns 403 to urllib's default User-Agent, so we
        # impersonate curl, which is what the worker provisioner uses.
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                served = r.read()
        except Exception as e:
            sys.exit(f"public URL not responding ({e}).\n"
                     f"Check the bucket's Public Development URL, or use --presigned.")
        if served != data:
            sys.exit("public URL serves different content than what was just uploaded "
                     "(R2 cache?). Retry in a few seconds or use --presigned.")
        print(f"public URL verified ({len(served)} bytes): {url}")

    # The webhook travels in the template environment, NOT inside the .sh: that
    # file is served from a public R2 URL and anyone could read it (and write
    # to the channel). If undefined, provisioning does not notify and that is
    # it. config.var() aborts if missing; this one is optional, so it is read
    # from the raw ENV.
    webhook = config.ENV.get("DISCORD_WEBHOOK", "").strip()
    env_webhook = f' -e DISCORD_WEBHOOK="{webhook}"' if webhook else ""
    print("Discord webhook:", "enabled" if webhook else "NOT set (no notifications)")

    template_env = (
        '-p 3000:3000 '
        '-e COMFYUI_ARGS="--disable-auto-launch --port 18188" '
        f'-e HF_TOKEN={HF_TOKEN} '
        '-e BENCHMARK_TEST_WIDTH=512 -e BENCHMARK_TEST_HEIGHT=512 '
        '-e BENCHMARK_TEST_STEPS=20 '
        f'-e PROVISIONING_SCRIPT="{url}"'
        f'{env_webhook}'
    )

    # note: update template changes the hash_id, it must be re-read afterwards
    vastai("update", "template", current_hash(),
           "--name", TEMPLATE_NAME,
           "--image", IMAGE, "--image_tag", IMAGE_TAG,
           "--ssh", "--direct", "--disk_space", DISK_SPACE,
           "--env", template_env,
           "--onstart-cmd", ONSTART,
           "--search_params", SEARCH_PARAMS,
           "--no-default")   # without forced verified=true
    new_hash = current_hash()
    print(f"template {TEMPLATE_ID} updated -> hash {new_hash}")

    # The search_params must be repeated HERE in addition to the template: if
    # they are only set in the template, the workergroup re-injects
    # verified=true on its own despite --no-default, and that leaves the pool
    # at 1 single offer -> the autoscaler relaxes the price ceiling and picks
    # expensive machines. With -n here, the stored search_query stays clean.
    vastai("update", "workergroup", str(WORKERGROUP_ID),
           "--endpoint_id", str(ENDPOINT_ID),
           "--template_hash", new_hash, "--template_id", str(TEMPLATE_ID),
           "--launch_args", "", "--gpu_ram", "16",
           "--search_params", SEARCH_PARAMS, "-n")
    print(f"workergroup {WORKERGROUP_ID} re-pointed (search_params forced)")

    if args.update_workers:
        vastai("update", "workers", str(WORKERGROUP_ID))
        print("worker rolling update launched")

    if args.presigned:
        print("\ndone. NOTE: this URL expires in 7 days.")
    else:
        print("\ndone. the URL is permanent, nothing to renew.")
    return 0


def current_hash() -> str:
    out = vastai("search", "templates", f"creator_id={config.entero('VAST_CREATOR_ID')}", "--raw")
    data = json.loads(out)
    templates = data.get("templates", data) if isinstance(data, dict) else data
    for t in templates:
        if t.get("id") == TEMPLATE_ID:
            return t["hash_id"]
    sys.exit(f"template {TEMPLATE_ID} not found")


if __name__ == "__main__":
    sys.exit(main())
