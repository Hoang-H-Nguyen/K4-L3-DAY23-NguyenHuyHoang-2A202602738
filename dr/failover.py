"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time
import os
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Ghi một sự kiện failover với timestamp vào JSONL."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "iso": datetime.now(timezone.utc).isoformat(), **kw}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
    print(json.dumps(record, ensure_ascii=False))
    return record


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Khôi phục và xác minh target trước khi chuyển edge sang region đó."""
    if target not in URL:
        raise ValueError("target phải là a hoặc b")
    if wait <= 0:
        raise ValueError("wait phải > 0")
    emit(step="1_verify_target", target=target, state=state_of(target))

    try:
        meta = snapshot.get(target, backend)
    except (Exception, SystemExit) as exc:
        emit(step="2_restore_snapshot", target=target, ok=False, error=str(exc))
        return {"ok": False, "target": target, "reason": f"snapshot restore failed: {exc}"}
    restored = pathlib.Path(f"state/region-{target}/vectors.sqlite")
    source = "b" if target == "a" else "a"
    primary = pathlib.Path(f"state/region-{source}/vectors.sqlite")
    rpo = snapshot.rpo(primary, restored)
    emit(step="2_restore_snapshot", target=target, ok=True,
         rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
         embed_model_version=meta.get("embed_model_version"), snapshot_at=meta.get("snapshot_at"),
         restored_at=meta.get("restored_at"))

    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("full\n")
    emit(step="3_scale_pool", target=target, pool_state="full")

    ready_deadline = time.monotonic() + wait
    last_body, last_status = {}, None
    while time.monotonic() < ready_deadline:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, max(0.1, wait)))
            last_status = response.status_code
            try:
                last_body = response.json()
            except ValueError:
                last_body = {"body": response.text[:200]}
            if response.status_code == 200:
                emit(step="4_wait_ready", target=target, ok=True, status_code=200,
                     waited_s=round(wait - max(0.0, ready_deadline - time.monotonic()), 2),
                     ready=last_body)
                edge = pathlib.Path("edge/active_region")
                edge.parent.mkdir(parents=True, exist_ok=True)
                edge.write_text(target + "\n")
                emit(step="5_dns_cutover", target=target, ok=True,
                     active_region=target,
                     ttl_s=float(os.environ.get("EDGE_TTL_SECONDS", "5")))
                return {"ok": True, "target": target, "state": state_of(target),
                        "rpo_seconds": rpo["rpo_seconds"], "docs_lost": rpo["docs_lost"],
                        "embed_model_version": meta.get("embed_model_version"),
                        "cutover": {"ok": True, "active_region": target}}
        except httpx.HTTPError as exc:
            last_body = {"error": type(exc).__name__}
        time.sleep(min(0.25, max(0.0, ready_deadline - time.monotonic())))
    emit(step="4_wait_ready", target=target, ok=False, status_code=last_status,
         waited_s=wait, ready=last_body)
    return {"ok": False, "target": target, "reason": "target did not become ready before timeout",
            "ready": last_body, "rpo_seconds": rpo["rpo_seconds"], "docs_lost": rpo["docs_lost"]}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
