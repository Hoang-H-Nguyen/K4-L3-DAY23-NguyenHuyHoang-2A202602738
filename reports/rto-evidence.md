# RTO/RPO Evidence — Lab 23

Các mốc là timestamp thật từ drill ngày 09/10/2026. `tools/measure_rto.py` lấy thời điểm bắt đầu request trong log loadgen, trừ `t_outage`, rồi làm tròn đến 0,1s.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| Outage bắt đầu | 2026-10-09T05:30:52 UTC | Kill Region A khi B alive | `chaos/chaos-events.jsonl:1` |
| Request lỗi đầu sau kill | +2.0s | `ok:false`, `ReadTimeout` đầu tiên có `ts >= t_outage` | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau kill | Không có | A vẫn down tới khi loadgen kết thúc | `reports/drill-1-nodr.jsonl:32` |
| RTO | **NO_RECOVERY**; 15 request lỗi | `recovered_by_region:null` | `reports/measure-drill-1.json:25`, `reports/measure-drill-1.json:28` |
| Restore A | Sau loadgen | Kill switch bare mode | `chaos/chaos-events.jsonl:2` |

## 2. Drill 2 — có DR

| Mốc | Giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| Outage bắt đầu | 0.0s | Kill A, B alive | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên sau kill | +2.0s | Request lỗi đầu có `ts >= t_outage` | `reports/drill-2-withdr.jsonl:26` |
| Checker phát hiện A UNHEALTHY | +15.0s | 3 probe lỗi liên tiếp, interval 5s | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +22.0s | RPO và model version ghi trong log | `reports/failover-events.jsonl:2` |
| Region B ready | +28.2s | `/readyz` trả 200 | `reports/failover-events.jsonl:4` |
| DNS/LB cutover sang B | +28.2s | Sau khi B ready | `reports/failover-events.jsonl:5` |
| Request thành công đầu tiên từ B | **+28.3s** | `ok:true`, `served_by:b` | `reports/drill-2-withdr.jsonl:39` |

Request tại `reports/drill-2-withdr.jsonl:25` bắt đầu 0,006s trước kill và kết thúc bằng timeout sau kill. Bộ đo chỉ xét request bắt đầu từ `t_outage`, nên dòng 26 là lỗi đầu hợp lệ.

| Chỉ số | Đo được | Mục tiêu | Kết luận | Evidence |
|---|---:|---:|---|---|
| RTO — inference API | **28.3s** | ≤ 300s | **PASS**; dư 271.7s | `reports/measure-drill-2.json:20`, `reports/measure-drill-2.json:22` |
| RPO — vector DB | **8.0s / 4 documents** | ≤ 300s | **PASS**; dư 292.0s | `reports/failover-events.jsonl:2` |
| Golden signals sau cutover | 10 request, p95 36.24ms, error rate 0 | 10 request thật | Đạt | `reports/runbook-run.jsonl:6` |

Kết quả đo có `valid:true`, `warnings:[]`, `killed_region:a`, `recovered_by_region:b` tại `reports/measure-drill-2.json:2`, `reports/measure-drill-2.json:4`, `reports/measure-drill-2.json:5` và `reports/measure-drill-2.json:6`.

## 3. Phân rã RTO

| Thành phần | Giây | Timestamp và cách tính | Cách giảm |
|---|---:|---|---|
| Health-check phát hiện | 15.0s | Outage → `UNHEALTHY`; `interval_s:5`, `threshold:3`, floor 15s tại `reports/health-events.jsonl:2` | Giảm interval sau khi kiểm tra tải probe và flapping |
| Runbook xác nhận outage + restore snapshot | 7.0s | `UNHEALTHY` → `3_scale_pool` tại `reports/failover-events.jsonl:3`; riêng `1_verify_target` → `2_restore_snapshot` chỉ khoảng 0,001s theo `reports/failover-events.jsonl:1` và `reports/failover-events.jsonl:2` | Rút ngắn xác nhận có circuit breaker; snapshot không phải nút thắt lần này |
| GPU pool warm-up | 6.2s | `3_scale_pool` → `4_wait_ready`; `waited_s:6.24` tại `reports/failover-events.jsonl:4` | Giữ pool B ấm sẵn, đổi lại chi phí GPU |
| DNS/LB cache và nhịp lấy mẫu | 0.1s | `5_dns_cutover` → request OK đầu tại `reports/drill-2-withdr.jsonl:39` | Giảm TTL nếu nhiều lần chạy chứng minh cache là nút thắt |
| **Tổng** | **28.3s** | 15.0 + 7.0 + 6.2 + 0.1 = 28.3s | Khớp `rto_measured_s` tại `reports/measure-drill-2.json:20` |

Khoảng 0,1s cuối không tách riêng được DNS cache khỏi lúc request mẫu tiếp theo bắt đầu. Khoảng 7,0s gồm thời gian từ health event đến lúc script gọi runbook, ba probe timeout của runbook và thao tác restore rất nhanh; không có thời gian người vận hành xác nhận vì drill dùng `--auto`.
