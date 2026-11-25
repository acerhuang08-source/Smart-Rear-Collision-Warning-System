# ioRadar701＋BNO055 Shadow 實作與驗證（2026-10-27）

> 2026-10-31 五輪實機資料的最終離線核對、同核心 replay、raw SHA、分類/reset 與場景
> 限制已續記於 `ioRadar701_BNO055_Shadow_實機測試結果_2026-10-31.md`；本文件以下保留
> 2026-10-27 實作與初次驗證歷史。
>
> 2026-11-01 軟體準備新增同程序非阻塞 `:event <detail>` 操作員標記；保存讀到完整
> stdin 行後的 host-monotonic 時間，replay 保留標記。SHADOW 仍不載入 calibration
> profile。現況與命令見 `docs/進度斷點_2026-11-01_ioRadar701.md`。

日期／時區：2026-10-27，Asia/Taipei
分支／HEAD：`codex/sensor-fusion-motion-classification`／
`3d6d71b5f1eb9d980d6a976d6ddf0ab4da9c0685`（dirty，未 commit）

## 目前階段與可執行命令

目前為：**操作員已完成一輪真實 ioRadar701＋BNO055 shadow live；其保存資料中的
同時間戳週期已完成程式核對、最小修正與離線 replay；本次修正工作沒有重新開啟
硬體，GPIO／LED 從未啟用。**

本次修正後重播該輪真實保存資料（只讀來源，不碰 UART/I²C/GPIO）：

```bash
cd /home/topics/Projects/Smart-Rear-Collision-Warning-System
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python \
  -m rear_warning.cli.ioradar701_shadow replay \
  shadow-results/live-20261027T213626.007934+0800 \
  --output-root shadow-results
```

真實 T4 capture replay（只讀來源，不碰 UART/I²C/GPIO）：

```bash
cd /home/topics/Projects/Smart-Rear-Collision-Warning-System
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python \
  -m rear_warning.cli.ioradar701_shadow replay \
  captures/ioradar701/capture-20261027T015513.515409+0800 \
  --output-root shadow-results
```

待操作員確認後手動開始的真實 shadow live：

```bash
cd /home/topics/Projects/Smart-Rear-Collision-Warning-System
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python \
  -m rear_warning.cli.ioradar701_shadow live \
  --port /dev/ttyAMA0 --baudrate 230400 \
  --i2c-bus 1 --bno055-address 0x29 \
  --confirm-hardware
```

live 命令會先列出設定與風險，再等空白 Enter。`--help`、非法參數、缺少確認、
非 TTY、EOF、非空輸入及 Enter 前 Ctrl+C 都是零硬體 I/O。按 Enter 後才：

1. 以唯讀 `fuser` preflight 檢查 UART/I2C owner；busy 明確退出，不 kill、不操作 service。
2. 建立新輸出目錄；以唯一 receive-only reader 開 `/dev/ttyAMA0` 230400/8N1，
   pySerial exclusive 加 Linux `TIOCEXCL`，不送 radar 命令。
3. 開 I2C bus 1/address `0x29`，沿用 `BNO055Device` identity、NDOF readiness、
   measurement 與 quality gate。
4. 以約 5 Hz 有界非阻塞固定面板或 `--plain` 顯示，所有畫面明列 SHADOW/GPIO disabled。
5. Ctrl+C 或期限結束時，分別嘗試關 UART、讓 BNO055 回 CONFIGMODE／關 bus、
   flush/close 記錄及還原終端；主要錯誤與 cleanup errors 分開保存。

操作員先前已用上述入口完成本文件後述的一輪 30 秒真實 shadow；本次同時間戳
修正工作沒有再次執行 live。現地參數已有該輪通訊證據，但 axis map、安裝方向、
calibration profile 與道路語意仍未驗證，因此不可把通訊成功擴張成整套警示系統
已驗收。

## 連接與無 GPIO 保證

- 雷達成功資料線維持藍板原 ESP32 插槽 IO16 → Pi 實體 Pin 10/GPIO15 RX；共地與
  供電沿用。Pi Pin 8/IO17 不接，舊 TFMini TX 隔離，不改用 P1 Pin 10。
- 雷達固定 `/dev/ttyAMA0`、230400/8N1、無流控、receive-only；不改用歷史上指向
  ttyAMA10 的 `/dev/serial0`。
- 新 CLI/core 沒有 import `rear_warning.outputs`、`gpiozero` 或 LED driver，沒有
  `--enable-led` 類參數，metadata 固定 `shadow=true`、`gpio_enabled=false`。
- 若要回退，只需停止新入口並確認它已關閉 ttyAMA0；standalone ioRadar、舊
  TFMini LIVE、service、接線與 calibration profile 都不需修改。

## 資料流、選擇與 reset

```text
單一 UART read -> receipt batch/time -> 共用 IoRadar701StreamParser
  -> RadarStateTracker cycle events -> RadarTargetSelector -> selected hypot(X,Y)
  -> 同批依序套用 reset，僅最後已封閉週期可更新 fusion
BNO055Device -> 現有 quality -> latest causal RadarMotionSynchronizer
  -> RadarRelativeMotionAdapter（既有 trend/own-motion/classifier）
  -> RelativeMotionAssessment + 獨立 WarningPolicy/stabilizer 候選
  -> 有界終端顯示 + JSONL/metadata/summary（無 GPIO）
```

週期只在三種閉合點發一次事件：宣告 N=0、N 個不同 ID 收齊，或下一 status 到達
而上一輪仍未收齊。合法收集中不 reset、不餵距離。selector 只消費完整新鮮週期；
目前 ID 仍存在就保持，否則按 `hypot(X,Y)` 最小、ID 平手排序。它保存候選集合與
原因，明確是開發策略，不代表威脅排序、車道或永久 identity。

target switch、N=0、封閉不完整、stale/error 與非法 cycle time 會清除 target-bound
distance trend、classifier baseline/candidate 與 warning stabilizer。健康 BNO window
與 motion timestamp dedup 保留，所以切 target 不會把同一 motion 再加權。BNO 自身
gap/invalid 則沿用 estimator/quality 的 not-ready 語意。X/Y/Vx/energy/raw 均保存；
只有 `hypot(X,Y)` 進入距離趨勢與 WarningPolicy，Vx 不作 closing rate/TTC/車速。

## 時間語意、接收批次與失效

- live 每次非空 `UART.read(4096)` 完成後只取一次 host-monotonic，該 chunk 是一個
  `receipt_batch_sequence`；parser 一次 feed 可以合法產生多個完整週期，所以同批
  週期可共用 receipt time。它不是 radar 內部量測時間。
- 同一批次仍逐輪執行 selector 與應有的 target switch／N=0／封閉不完整 reset，
  並在 `shadow.jsonl` 各留一列；只有最後一個已封閉週期可送入同步／距離融合。
  較早的可用週期記為 `fusion_update_coalesced`、`fusion_update=coalesced`，不製造
  epsilon 時間。最後週期不可用時不回退較早 target。
- 不同批次若時間相等，仍是 `equal_across_receipt_batches` 的時間不連續；真正倒退
  是獨立的 `backwards`。兩者都不送 estimator。大幅向前 gap 繼續由既有 estimator
  清除歷史並回報 `large_time_gap`。
- BNO canonical time：完整成功 read 後取得的 host monotonic，沿用既有同步層語意；
  `BNO055Measurement.monotonic_timestamp` 的 read-start time 另存，兩者不互填。
- 配對只取不晚於 range 的最新 valid motion；age 0 與 0.2 秒邊界合法，future、stale、
  invalid、unmatched 分開。重用同一 motion 可配多輪 range，但 own-motion 只更新一次。
- tick 在 UART 空讀時仍執行；只收到垃圾只更新 bytes freshness，不延長 status/target。
- `NO_TARGETS`、`INCOMPLETE_CYCLE`、`RADAR_STALE`、`RADAR_ERROR`、
  `RADAR_TIME_DISCONTINUITY`、`FUSION_UPDATE_COALESCED`、`BNO_NOT_READY`、
  `SYNC_UNMATCHED`、`FUSION_WARMUP` 與 `LIVE_SELECTED` 保持不同值。缺距離顯示
  unavailable，不補 0 m 或 SAFE。
- 相對分類與距離警示是兩欄獨立輸出；沒有 relative-class→LED mapping。

## Replay 來源、結果與限制

### 真實 shadow 同時間戳事件、根因與修正後 replay

來源 `shadow-results/live-20261027T213626.007934+0800/` 保持不動。原始 live 有
22464 raw bytes、594 status、1278 target、1872 decoded、272 BNO、594 shadow，
parser checksum/sync/trailing 全為 0，`recording_complete=true`、errors 與
cleanup_errors 皆空。原始 row 0／1 的 cycle 1／2 都是 `363.059385986`；row 1
因此被舊程式記為 `radar_time_discontinuity`，row 2 的 `363.08242669` 則繼續運作。
這是原始實機紀錄，沒有回寫或美化。

原始碼呼叫鏈確認根因是 live loop 對一個 `serial_device.read(4096)` 只呼叫一次
`_CLOCK()`，再把整個 chunk 與同一時間交給 `IoRadar701ShadowCore.feed_bytes`；共用
parser 與 `RadarStateTracker` 可在該次 feed 依序發出多個不同 cycle，沒有重複
emission 或 replay 重送。舊 core 卻逐輪把共用時間送進要求嚴格遞增的
`RadarRelativeMotionAdapter`／`DistanceTrendEstimator`，並在外層以單一
non-increasing 原因同時涵蓋相等與倒退。NTP、雷達內部時鐘與線材都不在這條因果鏈。

最小修正只在 shadow 輸入協調與其 capture/replay 邊界：live 保存明確 batch ID；
core 一個 feed 最多更新一次 fusion；所有 cycle 仍各有 audit row；跨批相等、倒退、
large gap 的既有失效語意保留。schema v2 是加欄相容：舊 v1 reader 可忽略新欄，
新 reader 仍可讀沒有 batch ID 的 v1 capture，但只能把連續完全相等的保存時間推定
為同批，metadata 明列這不是原 UART callback boundary 的直接證明。

修正後的純離線產物是
`shadow-results/replay-20261027T221839.269770+0800/`：1872 decoded、272 BNO、
594 shadow、625 個推定 receipt batches、592 次 fusion applied、1 次 coalesced、
0 次 radar time discontinuity；parser 計數與原檔完全一致，保存完整。原始與新結果
的 matched=589、invalid motion=3、sync null=2、fusion valid=578、ready=456、
baseline ready=205、stable_or_receding=448、uncertain=144、分類 null=2、warning=593
及 no-target=1 都不變；唯一 availability 差異是原本 1 次時間不連續改成 1 次可稽核
的同批 coalesced。首筆 invalid BNO 仍被拒絕，沒有回退舊 motion 或降低品質門檻。

### 先前六輪單雷達 replay（歷史）

六輪都用原 `raw.bin` 經共用 parser/state/core；`decoded.jsonl` 只提供當時保存的
host receipt elapsed。沒有同時段 BNO055 檔，因此 metadata 固定標示
`REPLAY／真實雷達＋無 BNO055 記錄`、`motion_source=unavailable`，不把缺動作補成
靜止。若來源只有 raw，程式才用明列的每有效幀 0.05 秒合成時間，標籤會改成
`REPLAY／真實雷達＋合成時間＋無 BNO055 記錄`。

| 來源 | 產物目錄 | frames / shadow | parser 品質 |
| --- | --- | ---: | --- |
| T1 014453 | `replay-20261027T171228.597171+0800` | 1804 / 597 | checksum/sync/trailing 0/0/0 |
| T3 014735 | `replay-20261027T171228.938621+0800` | 2728 / 596 | 0/0/0 |
| T4 015513 | `replay-20261027T171229.292645+0800` | 1381 / 589 | 0/1/0 |
| T6 120 s | `replay-20261027T171229.676127+0800` | 7030 / 2386 | 0/0/0 |
| restart 1 | `replay-20261027T171230.447266+0800` | 489 / 152 | 0/0/0 |
| restart 2 | `replay-20261027T171230.678050+0800` | 257 / 107 | 0/0/0 |

六輪 `recording_complete=true`，原 capture 未修改。T4 offset 0 的 `0xff` 仍由共用
parser 丟棄後恢復。當時的 v1 replay 把相同 timestamp 記為
`radar_time_discontinuity`；這一段保留為修正前歷史結果。因 v1 decoded 沒有 batch
ID，若以新程式再 replay，只能按連續相等 receipt timestamp 推定分組，不能冒稱已
完全重建當時 transport callback，也不能稱該時間為真實雷達量測時間。

實際輸出首筆例（T4）：

```json
{"availability":"no_targets","gpio_enabled":false,"reason":"status declared zero targets","shadow":true,"timestamp_seconds":0.45193444400047156}
```

## 保存檔 schema v2 與 v1 相容性

每次 live/replay 建立唯一目錄，不覆寫來源：

| 檔案 | 內容 |
| --- | --- |
| `raw.bin` | 僅 live：UART 完整原始 bytes。 |
| `decoded.jsonl` | 共用 parser 的 status/target raw 與縮放值，加 host receipt time 與 `receipt_batch_sequence`。 |
| `bno055.jsonl` | BNO 原始量、品質相關 state/calibration、raw read-start 與 canonical read-completed time；無 motion replay 為空檔。 |
| `shadow.jsonl` | availability、全部候選 ID、selected raw/X/Y/Vx/energy/hypot、選擇理由、sync/age、trend、own-motion、relative assessment、獨立 distance warning、reset 原因、batch ID、`fusion_update`／coalesced target cycle 與 GPIO=false。 |
| `events.jsonl` | lifecycle／invalid-motion 等事件；不是動作真值標註。 |
| `metadata.json` | 模式/來源、硬體請求與實際 BNO 狀態、兩種時間語意、selector、stale/age、Git dirty、shadow/GPIO、elapsed、errors、recording completeness。 |
| `summary.json` | 計數、parser、receipt batches、fusion applied/coalesced、time discontinuities、退出／cleanup 與保存完整性摘要。 |

schema v2 沒有刪除或改名 v1 欄位，只新增 batch／fusion disposition 與統計；舊資料
仍可 replay。v1 只有逐幀保存時間時，連續相同時間的 grouping 是明確標示的推定；
只有 raw bytes 時仍只能使用明列為 synthetic 的時間，不能還原真實 receipt time。

wall time 只用於目錄及 metadata；因果運算只用 monotonic。Pi 的 NTP 尚未同步，
metadata 不宣稱自動校時或跨機同步。任何 write/flush/close/metadata 錯誤都使
`recording_complete=false` 或非零退出，不把部分檔案報成完整保存。

## 本輪實際驗證

同時間戳修正的新增驗證全部是 fake／純資料／唯讀 capture，沒有
UART/I2C/GPIO/藍牙/systemd I/O。下列 318 是本輪實際重跑數，不沿用先前
63／455／1381：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/pytest -q \
  -p no:cacheprovider tests/ioradar701 \
  tests/synchronization/test_radar_motion.py tests/fusion tests/warning \
  tests/cli/test_ioradar701_shadow.py
# 318 passed in 2.12s
```

另實際執行三層 `--help`、兩檔 `py_compile`、真實保存資料 replay 及
`git diff --check`。新增測試涵蓋同批兩週期只更新一次、後續時間恢復、同批 target
switch／N=0／封閉不完整、跨批相等、真正倒退、large gap、motion dedup 與保存 BNO
的 causal replay；既有 Enter gate、Ctrl+C／partial-init cleanup、無 GPIO、fusion
與 warning regression 一併通過。本輪沒有為文件變更重跑 full suite。

## 未執行／待確認

- 本次修正後未重新執行真實 shadow live；新 batch ID 的 live 保存格式尚未由第二輪
  實機 capture 驗證。既有 30 秒真實輪只證明當時通訊與保存，不代表修正後道路驗收。
- 未執行 GPIO/LED、藍牙、systemd、道路、距離精度、T2/T5 或長時間驗收。
- radar X/Y 車體軸、Vx 符號、target ID 身分、rear corridor、switch margin、
  BNO axis/profile 與 calibration 最低門檻仍待確認。
- T3 仍只證明顯示值隨動，T4 本人 target 未辨識；本輪 replay 不改寫成場景通過。
- `docs/CODEX_PROJECT_TRANSFER_TASK.md` 與既有交接列出的 2026-10-18 缺件仍未取得。

## 停止與回退

live 按 Ctrl+C；正常時回傳 130 並保存 interrupted/full capture，cleanup 失敗則
回傳 1。若 terminal 仍存在先核對 summary 的 `recording_complete`、errors 與
cleanup_errors，再確認 ttyAMA0 owner。回退只需停止此新程序；不要刪 lock、不要
自動 stop/disable service。舊 standalone ioRadar、TFMini LIVE 及原 capture 不受影響。
