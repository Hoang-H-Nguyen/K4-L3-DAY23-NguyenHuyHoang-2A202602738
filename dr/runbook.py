"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi một bước runbook cùng timestamp vào JSONL."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
              "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
    print("RUNBOOK", json.dumps(record, ensure_ascii=False))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Tự xác nhận trong CI hoặc hỏi operator theo mặc định y/N."""
    return True if auto else input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Thực hiện bảy bước ứng phó và ghi timeline incident."""
    if primary not in URL or target not in URL or target == primary:
        raise ValueError("primary/target phải là a hoặc b và phải khác nhau")
    started = time.time()
    # Multiple probes make this an operator check rather than trusting one sample.
    observations = []
    for _ in range(3):
        try:
            response = httpx.get(f"{URL[primary]}/readyz", timeout=2.0)
            observations.append(response.status_code == 200)
        except httpx.HTTPError:
            observations.append(False)
        time.sleep(0.2)
    if all(observations):
        step(1, "xac_nhan_outage", primary=primary, target=target,
             confirmed=False, reason="primary readiness probes succeeded", probes=observations)
        return {"ok": False, "reason": "primary still ready; refusing failover"}
    if observations.count(False) < 2:
        step(1, "xac_nhan_outage", primary=primary, target=target,
             confirmed=False, reason="outage not confirmed by 2 of 3 probes", probes=observations)
        return {"ok": False, "reason": "outage not confirmed", "probes": observations}
    try:
        target_response = httpx.get(f"{URL[target]}/healthz", timeout=2.0)
        target_alive = target_response.status_code == 200
    except httpx.HTTPError:
        target_alive = False
    if not target_alive:
        step(1, "xac_nhan_outage", primary=primary, target=target,
             confirmed=True, target_alive=False, probes=observations)
        step(2, "thong_bao_incident", primary=primary, target=target,
             outage_ts=None, operator_notified_ts=time.time(), clock_started_ts=started)
        step(3, "scale_gpu_pool", ok=False, reason="target region is not alive")
        step(4, "verify_state_replica", ok=False)
        step(5, "dns_cutover", ok=False, cutover=None)
        step(6, "verify_golden_signals", ok=False, requests=0, errors=0, error_rate=None)
        step(7, "post_incident", elapsed_s=round(time.time() - started, 2),
             outcome="aborted: target region is not alive")
        return {"ok": False, "reason": "target region is not alive"}
    step(1, "xac_nhan_outage", primary=primary, target=target, confirmed=True,
         target_alive=True, probes=observations)

    chaos_lines = []
    chaos_path = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_path.exists():
        for line in chaos_path.read_text().splitlines():
            try:
                event = json.loads(line)
                if event.get("action") == "kill" and event.get("region") == primary:
                    chaos_lines.append(event)
            except json.JSONDecodeError:
                continue
    outage_ts = chaos_lines[-1]["ts"] if chaos_lines else None
    notice_ts = time.time()
    notice = step(2, "thong_bao_incident", primary=primary, target=target,
                  outage_ts=outage_ts, operator_notified_ts=notice_ts,
                  clock_started_ts=started)

    if not confirm(auto, f"Đã xác nhận {primary} outage; chuyển traffic sang {target}?"):
        step(3, "scale_gpu_pool", ok=False, cancelled=True)
        step(4, "verify_state_replica", ok=False, skipped=True)
        step(5, "dns_cutover", ok=False, skipped=True)
        step(6, "verify_golden_signals", ok=False, skipped=True, requests=0)
        step(7, "post_incident", elapsed_s=round(time.time() - started, 2),
             outcome="cancelled by operator")
        return {"ok": False, "reason": "operator declined failover"}
    result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=result.get("ok"), failover=result)
    if not result.get("ok"):
        step(4, "verify_state_replica", ok=False)
        step(5, "dns_cutover", ok=False, cutover=result.get("cutover"))
        step(6, "verify_golden_signals", ok=False, skipped=True, requests=0)
        step(7, "post_incident", elapsed_s=round(time.time() - started, 2),
             outcome=result.get("reason", "failover failed"),
             rpo_seconds=result.get("rpo_seconds"), docs_lost=result.get("docs_lost"))
        return result

    state = result.get("state", {})
    step(4, "verify_state_replica", ok=bool(state.get("weights") and state.get("count", 0) > 0),
         vectors=state.get("count"), weights=state.get("weights"),
         embed_model_version=result.get("embed_model_version"))
    step(5, "dns_cutover", ok=result.get("cutover", {}).get("ok", False),
         active_region=result.get("cutover", {}).get("active_region"))

    latencies, errors = [], 0
    for i in range(10):
        t = time.perf_counter()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=5.0)
            latencies.append((time.perf_counter() - t) * 1000)
            if response.status_code != 200 or response.json().get("region") != target:
                errors += 1
        except (httpx.HTTPError, ValueError):
            latencies.append((time.perf_counter() - t) * 1000)
            errors += 1
    p95 = sorted(latencies)[max(0, int(0.95 * len(latencies) + 0.999) - 1)] if latencies else None
    step(6, "verify_golden_signals", target=target, requests=10, p95_ms=round(p95, 2) if p95 is not None else None,
         error_rate=errors / 10, errors=errors)
    summary = {"rto_command": "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300",
               "elapsed_s": round(time.time() - started, 2), "rpo_seconds": result.get("rpo_seconds"),
               "docs_lost": result.get("docs_lost"), "p95_ms": round(p95, 2) if p95 is not None else None,
               "error_rate": errors / 10}
    step(7, "post_incident", **summary)
    return {"ok": True, **summary, "failover": result, "notice_ts": notice["ts"]}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
