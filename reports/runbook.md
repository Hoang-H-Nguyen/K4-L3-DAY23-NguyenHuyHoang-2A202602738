# Runbook — Region chính down

Chạy từ thư mục gốc dự án. Region B ban đầu `warm`, chưa có snapshot phục hồi nên `/readyz` trả 503 là bình thường; chỉ cần B trả 200 ở `/healthz` trước khi gọi runbook. Lệnh ở bước 2 thực hiện một lần toàn bộ năm bước failover và các bước kiểm tra còn lại.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage và target còn sống | `python3 chaos/kill_region.py status`; `curl -i http://127.0.0.1:8002/healthz`; `rg '"region": "a", "to": "UNHEALTHY"' reports/health-events.jsonl | tail -n 1` | Checker ghi `region:a`, `to:UNHEALTHY`, `consecutive_fails >= 3`; B `/healthz` trả 200. | On-call SRE |
| 2 | Mở incident, thông báo và bắt đầu đồng hồ | `python3 dr/runbook.py --primary a --target b --backend fs` | Xác nhận `y`; có `thong_bao_incident` timestamp trong `reports/runbook-run.jsonl`. Khi chạy drill, đồng hồ outage lấy từ `chaos/chaos-events.jsonl`. | Incident commander |
| 3 | Khôi phục snapshot, scale GPU và đợi target ready | `tail -n 5 reports/failover-events.jsonl`; `curl -i http://127.0.0.1:8002/readyz` | Runbook bước 2 đã gọi failover một lần; log có `1_verify_target` đến `4_wait_ready` theo thứ tự, RPO/docs lost/model version; B `/readyz` trả 200. Nếu thiếu snapshot, dừng và escalation, không chụp snapshot mới từ A đang lỗi. | On-call SRE / ML platform |
| 4 | Kiểm tra replica state | `curl -s http://127.0.0.1:8002/v1/state` | `count > 0`, `weights: true`; log `verify_state_replica` có `ok: true`. | ML platform |
| 5 | Chuyển DNS/LB sau khi ready | `curl -s http://127.0.0.1:8080/edge/state`; `tail -n 1 reports/failover-events.jsonl` | `active_region` là `b`; dòng cuối là `5_dns_cutover`, xuất hiện sau `4_wait_ready`. | On-call SRE; incident commander phê chuẩn |
| 6 | Kiểm tra golden signals | `tail -n 2 reports/runbook-run.jsonl`; `curl -s http://127.0.0.1:8002/v1/infer` | Runbook ghi 10 request thật trong `verify_golden_signals`, error rate = 0 và p95 để đối chiếu baseline. | On-call SRE + ML platform |
| 7 | Đo RTO/RPO và viết postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid: true`, `rto_verdict: PASS`, RTO ≤ 300s; RPO và docs lost đối chiếu với `2_restore_snapshot`. Lưu số thật cùng path:line trong báo cáo. | Incident commander |

**Rollback:** chỉ chuyển traffic về A sau khi A đã được khôi phục, `/readyz` trả 200 liên tiếp ít nhất 3 lần cách nhau 5 giây, state/model version được xác nhận tương thích và golden signals ổn định. Incident commander quyết định rollback; on-call SRE thực hiện và ghi timestamp. Nếu B lỗi trước khi chuyển, dừng failover và giữ traffic/incident ở trạng thái hiện tại; không ghi DNS sang target chưa ready.

**Chuẩn bị trước sự cố:** `bash scripts/up_bare.sh`; đảm bảo A và B alive; chạy `python3 state/replicate.py --every 30 --duration 3600 --backend fs` liên tục để snapshot sẵn có. Mọi thao tác dùng `--backend fs` trong bare mode.
