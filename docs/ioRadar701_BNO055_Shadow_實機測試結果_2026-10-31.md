# ioRadar701＋BNO055 SHADOW 實機測試最終離線核對（2026-10-31）

工作階段／時區：2026-10-31，Asia/Taipei

分支／HEAD：`codex/sensor-fusion-motion-classification`／
`3d6d71b5f1eb9d980d6a976d6ddf0ab4da9c0685`；worktree 起始與結束皆 dirty，
保留既有 tracked／untracked WIP，沒有 stage、commit、push、stash、clean 或 restore。

本輪只做保存檔核對、同核心 replay、純資料分析、hardware-free regression 與文件更新。
沒有開 UART／I²C／GPIO／藍牙，沒有操作 systemd、NTP、校正設定或分類門檻。原 capture
名稱保持不變；它們的 Pi wall-clock 日期不作跨機同步證據。五輪 replay 在午夜後建立，
所以新輸出目錄名為 `20261101T...+0800`，不反向改寫工作階段或來源檔名。

## 結論

- 五輪來源各有七檔：`raw.bin`、`decoded.jsonl`、`bno055.jsonl`、`shadow.jsonl`、
  `events.jsonl`、`metadata.json`、`summary.json`。JSON／JSONL 全部可讀；raw size、parser、
  行數、metadata counts 與 summary 相符。五輪皆 `recording_complete=true`，errors 與
  cleanup_errors 空，原檔未修改。
- 使用目前 `rear_warning.cli.ioradar701_shadow replay` 逐輪重播到新目錄。每輪 replay
  的 decoded／BNO／shadow／events 四個 JSONL 都和 live 來源逐 byte、逐 SHA-256 相同；
  replay summary/parser/counts 逐欄一致，`batching_source=saved_receipt_batch_sequence`。
- 每個 receipt batch 至多一筆 `fusion_update=applied`；五輪各有 1 筆 coalesced，全部
  `radar_time_discontinuities=0`。所有 shadow rows 都是 `shadow=true`、
  `gpio_enabled=false`。
- 所有 `matched` BNO 配對都滿足 motion timestamp 不晚於 radar timestamp、保存 age
  等於兩時間戳之差且不超過 0.2 秒；五輪最大 age 為 0.10217–0.10482 秒，違規 0。
- 每輪第一筆 BNO 都因 quaternion norm 與 gravity norm 為 0 而被拒絕並保存一筆
  `invalid_motion` event；後續通過目前 broad integrity quality 的筆數分別為
  550、2756、828、274、826。這不是 calibration 通過：system／accelerometer／
  magnetometer calibration 全程為 0；gyroscope 除首輪前 8 筆及其餘各輪第一筆為 0，
  其餘為 3。
- 第三輪只能按操作員確認記為「只有雷達平移、旋轉，Pi／BNO055 固定」，不能作共同
  運動融合驗收。操作員已確認第五輪實際為全部設備固定、人員正面移動；場景確認已
  結案且不需重測，但缺少 stage event／target identity 真值，不能把分類當成行為驗收。

## 五輪保存與 replay 結果

| 輪次／來源 | 實際 elapsed／退出 | raw bytes／SHA-256 | decoded（status＋target） | BNO（有效＋無效） | shadow disposition（applied／coalesced／other） |
| --- | --- | --- | ---: | ---: | ---: |
| 1 `live-20261027T234216.925474+0800` | 60.067886 s／completed | 41448／`40c10a835b8777f0b8ac4602f151cd9df04cc8045aa34e079717e46db6c213f3` | 3454（1194＋2260） | 551（550＋1） | 1194（1192／1／1 no-target） |
| 2 `live-20261028T000218.800241+0800` | 300.060761 s／completed | 143232／`3d4fe631529860bb4ddc5a2e1c4e915fca2b63767a77cb316870b94cda468592` | 11936（5968＋5968） | 2757（2756＋1） | 5968（5967／1／0） |
| 3 `live-20261028T003818.324819+0800` | 90.317005 s／Ctrl+C，requested 120 s | 63348／`3fa448f4756124d01f2cb2354e8157dc60d0774b91a0639f9ec0a82f446ec8fa` | 5279（1795＋3484） | 829（828＋1） | 1795（1781／1／13 no-target） |
| 4 `live-20261028T004000.032672+0800` | 30.065590 s／completed | 19444／`e5a9e2a025cbf7db09d3f51beba58e419574bec05cf2b74a2e9f615798b07d20` | 1620（597＋1023） | 275（274＋1） | 596（594／1／1 no-target） |
| 5 `live-20261028T005242.998440+0800` | 90.064809 s／completed | 68676／`83029b7e69c23aad002e9ac2b1f6040ba6a9b8583d72dc8f93da18dc1960ec02` | 5723（1789＋3934） | 827（826＋1） | 1789（1782／1／6 no-target） |

五輪 checksum failure 與 sync discard 均為 0。第 1、2、3、5 輪 trailing bytes 為 0；
第 4 輪為 4。第 4 輪最後一個合法 status（cycle 597）宣告 2 targets，但 capture 結束前
沒有收齊；最後一筆已輸出的 shadow 是 cycle 596。四個 trailing bytes 與這個未封閉週期
都由 replay 原樣重現。`recording_complete=true` 表示保存與 cleanup 完成，不表示最後
一個雷達週期必定封閉。

replay 對照：

| 來源 | 新 replay 目錄 |
| --- | --- |
| 第 1 輪 | `shadow-results/final-offline-20261031/replay-20261101T000032.717622+0800/` |
| 第 2 輪 | `shadow-results/final-offline-20261031/replay-20261101T000033.424386+0800/` |
| 第 3 輪 | `shadow-results/final-offline-20261031/replay-20261101T000035.347250+0800/` |
| 第 4 輪 | `shadow-results/final-offline-20261031/replay-20261101T000036.073881+0800/` |
| 第 5 輪 | `shadow-results/final-offline-20261031/replay-20261101T000036.502119+0800/` |

## v2 batch、因果配對、分類與 reset

| 輪次 | receipt batches | sync：matched／invalid／none | 最大 matched age | fusion classification | reset 摘要 |
| --- | ---: | ---: | ---: | --- | --- |
| 1 | 1253 | 1189／3／2 | 0.104821 s | stable 1160；uncertain 32 | initial 2；no-target 1 |
| 2 | 6125 | 5965／2／1 | 0.102278 s | stable 5795；uncertain 172 | initial 1 |
| 3 | 1882 | 1778／3／14 | 0.102259 s | stable 1312；uncertain 469 | initial 11；no-target 13；target switch 7 |
| 4 | 627 | 591／3／2 | 0.102170 s | stable 555；uncertain 39 | initial 2；no-target 1 |
| 5 | 1888 | 1780／2／7 | 0.102373 s | stable 1055；uncertain 714；static/self 13 | initial 7；no-target 6；target switch 5 |

`none` 都對應 coalesced 或 no-target 等不應進融合的列。`invalid` 是啟動第一筆無效 BNO
在下一筆有效資料到達前影響的 radar cycles；沒有拿舊有效資料冒充。分類是現行演算法
對保存資料的 deterministic 輸出，不是事件／目標身份／方向真值，也不能從表中宣稱
距離精度、道路警示或人物接近分類通過。

第 3 輪 target switches：0→2 一次、1→0 一次、1→2 兩次、2→1 三次。第 5 輪：
0→1 兩次、1→4、2→4、4→2 各一次。每次 switch 都清 target-bound trend/classifier；
健康 BNO window 保留並以 timestamp 去重。no-target 會清 selector、target trend、
classifier 與 warning stabilizer；BNO invalid 則走 `reset_all`。

### 目前批次修正與失效防護核對

- `IoRadar701ShadowCore._process_receipt_batch` 仍依序做 cycle selection/reset，但只有同批
  最後一個已封閉 cycle 能進 `_process_selected`；先前 selected cycles 只產生
  `fusion_update_coalesced` audit row。若最後 cycle 是 N=0 或 incomplete，不會回退拿同批
  較早 target 更新融合。
- `_last_closed_cycle_timestamp` 保留跨批邊界；跨批相等與 backwards 分別標示並拒絕，
  distance/own-motion/classifier 的 large gap 仍走明確 discontinuity/reset，沒有 epsilon、
  假 50 ms 或 wall-clock 補值。
- invalid BNO 會由 synchronizer 記成最新失效樣本，阻止較舊 valid BNO 被誤配；恢復後才
  重新 matched。radar adapter 只讓一個 canonical BNO timestamp 更新 own-motion 一次，
  重複 timestamp 若 payload 衝突會拒絕。
- target switch、no-target、incomplete、stale、radar error 與 BNO invalid 的 reset 範圍
  維持分層；健康 BNO window 不因 target switch 被重複計數。Capture 對各檔 flush/fsync/
  close，任何 storage 或 cleanup error 都會讓完整性失敗。
- replay 路徑不呼叫 hardware preflight；`ShadowAssessment` 固定 `gpio_enabled=false`，入口
  沒有 GPIO adapter 或 enable flag。本輪 replay 與 318 項 regression 均覆核上述邊界。

## 首輪差額與約第 47 秒暖機

首輪 `1194 shadow - 1192 applied - 1 coalesced` 的剩餘一列是：

- `shadow.jsonl` 第 939 列，cycle 939、receipt batch 980；timestamp
  `4561.111365206`。
- 相對第一筆 invalid BNO event 約 47.074538 秒，內容為
  `availability=no_targets`、`fusion_update=not_applicable`、
  `reset_reason=status declared zero targets`。
- 這是 radar status 明確宣告 N=0，不是遺漏 disposition，也不能指認為特定人離開。

cycle 940 在下一批重新取得 ID 0，標成 `initial target acquisition`。cycle 940–945 的
原因是 `distance_trend_not_ready`，cycle 946–952 是
`candidate_persistence_not_satisfied`；cycle 953 恢復 `live_selected`。這段約 0.65 秒的
暖機是 no-target 正確清除 target-bound trend／classifier 後重建造成。own-motion
unique sample count 在 cycle 940–952 都維持 10，sync 也 matched，因此不是 BNO 掉線、
BNO 視窗被誤清、NTP 或共同運動造成。

## 實際命令與本輪 regression

五輪 replay 使用同一命令形式，逐輪替換 `<capture>`：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python \
  -m rear_warning.cli.ioradar701_shadow replay \
  shadow-results/<capture> \
  --output-root shadow-results/final-offline-20261031
```

本輪實際重跑的 hardware-free regression：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/pytest -q \
  -p no:cacheprovider tests/ioradar701 \
  tests/synchronization/test_radar_motion.py tests/fusion tests/warning \
  tests/cli/test_ioradar701_shadow.py
# 318 passed in 2.18s
```

這個 318 才是本輪結果；舊文件的 63／455／1381 只保留為歷史。本輪沒有重跑 full
suite，因 runtime code 與門檻未改，範圍集中在 batch、parser、sync、fusion、warning、
shadow replay 與 fake lifecycle regression。

## 限制、未完成與下一步

現地仍缺少 `docs/CODEX_PROJECT_TRANSFER_TASK.md`、既有交接所述兩份 2026-10-18
ioRadar 文件、512-byte evidence，以及 `docs/evidence/ioradar701/` 四份 Windows JSON；
因此不宣稱 Windows／Pi 檔案已同步或跨機對時。缺件不影響本輪對現地七檔與 replay 的
可重現核對。

五輪 events 都只有一筆啟動時 invalid BNO，沒有場景階段、事件真值或 target identity
標記。尚未驗證：距離精度、X/Y/Vx 物理方向、車體座標、ID 持續身份、共同運動融合、
道路場景、LED／GPIO 決策、藍牙、systemd、NTP、校正部署與分類門檻。第 2 輪只證明
固定設備條件下資料鏈可保存／重現；第 3 輪不能作共同運動驗收。第 5 輪場景已由
操作員確認為全部設備固定、人員正面移動，但仍需事件／身份真值才能作行為驗收。

若後續要驗收融合共同運動，另設有明確 stage event／target identity 的受控場景，讓
雷達與 Pi／BNO055 按同一剛體一起移動，並先決定 calibration 最低門檻。任何新 live、
GPIO 或 service 測試都必須另行說明命令、預期效果與風險並取得確認；本次三小時
SHADOW 測試段落已結案，本輪到此停止。
