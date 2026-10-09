# Postmortem — DR Drill Lab 23

Drill giả lập Region A ngừng trả lời bằng `netblock --mock`. Region B được khôi phục từ snapshot, chờ inference ready rồi nhận traffic. Đánh giá này tập trung vào quy trình và hệ thống.

## 1. Timeline

| UTC | Sự kiện | Evidence |
|---|---|---|
| 05:31:42 | Outage bắt đầu; A bị netblock, B alive | `chaos/chaos-events.jsonl:3` |
| 05:31:44 | Request đầu sau kill bị timeout | `reports/drill-2-withdr.jsonl:26` |
| 05:31:57 | Health checker đánh dấu A UNHEALTHY sau 3 lỗi | `reports/health-events.jsonl:2` |
| 05:32:04 | Runbook `--auto` xác nhận outage và ghi mốc incident | `reports/runbook-run.jsonl:1`, `reports/runbook-run.jsonl:2` |
| 05:32:04 | Snapshot restore xong; RPO 8.0s, mất 4 docs | `reports/failover-events.jsonl:2` |
| 05:32:10 | B ready rồi DNS cutover | `reports/failover-events.jsonl:4`, `reports/failover-events.jsonl:5` |
| 05:32:11 | Request đầu thành công từ B | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO và gap so với mục tiêu

- **RTO:** mục tiêu 300s, đo được **28.3s**, dư 271.7s; 13 request sau kill lỗi. Kết quả `valid:true`, `warnings:[]` tại `reports/measure-drill-2.json:2`, `reports/measure-drill-2.json:4`, `reports/measure-drill-2.json:20`, `reports/measure-drill-2.json:25`.
- **RPO:** mục tiêu 300s, đo được **8.0s / 4 documents lost**, dư 292.0s. B giữ 216 vectors, weights và `embed-model=vi-e5-base@v3`; evidence tại `reports/failover-events.jsonl:2` và `reports/runbook-run.jsonl:4`.
- **Gap lớn nhất trong RTO:** health detection 15.0s, khoảng 53.0% RTO. Kế tiếp là bước xác nhận tự động của runbook và restore 7.0s, warm-up 6.2s. Phân rã tại `reports/rto-evidence.md`.

## 3. Root cause (5 whys)

1. User nhận lỗi vì edge vẫn trỏ tới A khi A ngừng trả lời.
2. Edge chờ health checker và runbook xác nhận trước khi đổi vùng; drill này chạy runbook với `--auto`, còn vận hành thường yêu cầu operator xác nhận.
3. Checker đợi 3 probe lỗi liên tiếp để tránh flapping, tạo detection floor 15s.
4. B ban đầu thiếu state và chỉ có pool warm; restore snapshot và warm-up là điều kiện trước cutover.
5. Kiến trúc active-passive giảm chi phí GPU thường trực và yêu cầu cutover có kiểm soát; vì vậy RTO gồm các pha chờ thật. Lần đo này vẫn nằm trong 300s.

Trong outage thật, runbook có thể dừng nếu snapshot thiếu/quá cũ, model version không tương thích hoặc B không trả 200 ở `/readyz`. Khi đó failover phải abort trước DNS và on-call escalation.

## 4. Action items

| # | Hành động | Owner | Deadline | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Cảnh báo khi snapshot chậm hơn lịch replicate 30s; giám sát manifest, vectors và model version | ML platform | Trước game day tiếp theo | Giới hạn RPO và phát hiện backup lỗi sớm |
| 2 | Đo nhiều drill với interval 3s, threshold 3; so sánh flapping, tải probe và RTO | SRE | Trước game day tiếp theo | Có thể giảm detection floor từ 15s xuống 9s, tối đa 6s nếu các pha khác giữ nguyên |
| 3 | Rút ngắn bước xác nhận khi checker đã có 3 lỗi và B alive, vẫn giữ quyền phê chuẩn cutover | Incident commander | Trước game day tiếp theo | Giảm một phần pha xác nhận 7.0s; cần đo lại để biết số thật |

## 5. Câu hỏi học tập

1. `interval × threshold = 5s × 3 = 15s`, chiếm khoảng **53.0%** RTO 28.3s. Để đạt mục tiêu 300s, interval phải thỏa `interval × 3 < 300s − thời gian xác nhận/restore/warm-up/DNS`; chia 300/3 mà bỏ các pha khác sẽ sai.
2. Interval 1s với threshold 3 cho detection floor 3s, giảm lý thuyết 12s. Đổi lại số probe tăng 5 lần và các lỗi ngắn dễ liên tiếp đạt threshold hơn; cần đo tải và flapping trước khi đổi.
3. Nếu A mất vĩnh viễn 6 giờ, `docs_lost:4` nghĩa là bốn cập nhật đã ingest ở A nhưng chưa nằm trong snapshot B lúc restore. Khách hàng có thể không tìm thấy chúng; cần nhận diện, tái nhập từ nguồn gốc và thông báo phạm vi tác động. Số mất sẽ khác nếu replication ngừng lâu hơn.
