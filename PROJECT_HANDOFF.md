# Project Handoff

## 2026-11-25：ioRadar701 自啟動服務（已安裝並啟用；重開機自啟已驗證）

- 問題：開機自啟動的 `tfmini.service` 仍是舊 TFMini 程式（系統 unit
  `/etc/systemd/system/tfmini.service`，`User=topics`，`ExecStart=/usr/bin/python3
  /home/topics/Desktop/python/20260713test.py`，`Restart=always`，enabled）。該腳本在
  專案目錄外，依規範未讀取。車上距離感測器已是 ioRadar701，它開機後會占用
  `/dev/ttyAMA0`。
- 現有 `ioradar701_led` 不能直接交給 systemd：需要互動 TTY 與 Enter；`_run_live`
  只處理 Ctrl+C，SIGTERM 會讓 `finally` 不執行、燈可能留著；完整保存約 7 MB/分鐘
  （約 10 GB/天，剩 34 GB）。
- 操作員決定：資料採有上限的輪替保存；BNO055 初始化失敗就讓整個服務非零結束，
  交給 systemd 重啟；先實作程式與服務檔，安裝與啟用另行確認。
- 已實作（未改 selector、門檻、遲滯、LED 規則或 parser）：
  - `src/rear_warning/cli/ioradar701_shadow.py`：把收尾寫 metadata／summary 的程式
    抽成 `_finalize_capture`，輸出內容不變；`_run_live` 新增選用參數
    `capture_prefix`、`segment_seconds`、`on_capture_created`、`on_capture_closed`、
    `operator_input`。全部有預設值，不傳時行為與原本相同。切段時不重開
    UART／I²C／GPIO：先建新目錄再收尾舊段，舊段有自己的 metadata／summary，
    並帶 `segment`（index、final、segment_seconds、run_started_at；parser 與
    receipt_batches 為自 run 開始累計）。
  - 新增 `src/rear_warning/cli/ioradar701_service.py`：無 TTY、無 Enter，仍必須帶
    `--confirm-hardware --enable-led`。SIGTERM／SIGHUP 第一次轉成既有中斷流程
    （全關、關閉 GPIO／UART／I²C、收尾後 exit 130），之後重複的訊號忽略。
    預設每 600 秒切段，存到 `local-state/autostart-ioradar701/`（已被 .gitignore 排除），
    上限 2 GiB；只刪除本服務建立、名稱為 `service-*` 且帶 `.rear-warning-autostart`
    標記檔的最舊目錄，正在寫的那段不刪。BNO055 失敗與 UART 被占用都是 exit 1，
    不打開被占用的 UART、不 kill owner。
  - 新增 `config/systemd/user/rear-warning-ioradar701.service`（使用者層級，沿用
    autoconnect 模式；linger=yes，topics 已在 dialout／i2c／gpio 群組）：參數同今日
    控燈命令，`Restart=on-failure`、`RestartSec=5`、`SuccessExitStatus=130`、
    `TimeoutStopSec=10`、`StartLimitIntervalSec=0`、`WantedBy=default.target`。使用者
    unit 無法 `Conflicts=` 系統 unit，所以必須先停用 tfmini。
  - `pyproject.toml` 新增 `ioradar701-service` console script。
  - 新增 `tests/cli/test_ioradar701_service.py`，11 個測試全用假硬體：缺開關參數不碰
    硬體；無 TTY 可啟動且不讀 stdin；真實 SIGTERM 會全關、全部 close、收尾完整並
    還原 signal handler；切段時 GPIO／UART 各只開一次，startup 只在首段、cleanup
    只在末段；BNO 失敗 exit 1 並釋放 GPIO；UART 被占用 exit 1、不開 UART、不建目錄；
    prune 只刪最舊的標記目錄、不刪目前段；不切段時 metadata 沒有 segment 欄位。
- 驗證：既有相關測試改前改後都是 105 passed；新測試 11 passed；full suite
  1,473 passed。語法檢查、服務 `--help`（exit 0）、`systemd-analyze --user verify`
  （exit 0）、`git diff --check` 都通過。專案沒有 mypy／ruff，所以沒有跑型別檢查。
  `ioradar701_shadow.py` 原本就未追蹤，`git diff` 看不到這次修改。沒有開 UART／
  I²C／GPIO，沒有操作 systemd，沒有 stage／commit／push，仍在
  `codex/sensor-fusion-motion-classification`。
- 實機驗證與安裝（以下操作都由操作員在 Pi 終端執行）：
  - 停用 tfmini：操作員自行執行 `sudo systemctl disable --now tfmini.service`。16:46:32
    核對為 inactive/dead、MainPID=0、**UnitFileState=disabled**；unit 檔與 Desktop 腳本
    都保留，可用 `sudo systemctl enable --now tfmini.service` 還原（需先退回新服務）。
  - 16:37:18 測前盤點：UART／I²C 無 owner，GPIO17/22/27 為 input、無 consumer。
  - V1 手動 60 秒，`hardware-results/autostart-validation-20261125/V1/
    service-20261125T163947.201423+0800`：exit 0、60.19 s、complete=true、
    interrupted=false，errors／cleanup 空，全關與 GPIO close 成功；SAFE 823、WARNING 111、
    DANGER 252，GPIO 寫入 21 次。parser 出現 checksum 失敗 4 次、sync 丟棄 48 bytes
    （trailing 0），這幾天實測首次出現；parser 有重新同步，原因未判定。
  - V2 SIGTERM，`…/V2/service-20261125T164528.858954+0800`：跑 26.70 s 後由操作員在
    另一終端 `pkill -TERM` 停止；終端顯示 SIGTERM，`echo` 看到 exit 130；
    complete=true、interrupted=true，errors／cleanup 空，全關與 GPIO close 成功；
    SAFE 525，parser 錯誤 0。
  - V1／V2 操作員目視：V1 晃動時有變燈，兩輪結束後六燈全熄。三種判定（程式、GPIO
    收尾、目視）都通過。16:46:32 測後盤點：UART／I²C 無 owner，GPIO 為 input、
    無 consumer，沒有殘留程序。
  - 16:55 安裝：`systemctl --user link` 把 unit 連結到
    `~/.config/systemd/user/rear-warning-ioradar701.service`，接著 `daemon-reload`、
    `enable --now`。操作員貼上指令時最後一行 `echo` 引號不完整，停在 `>`，但前面的
    指令都已完成。16:57 核對：enabled、active (running)、MainPID 20331、NRestarts 0；
    UART／I²C 只被該 PID 占用，GPIO17/22/27 為 output、consumer=`lg`。
  - 這台 Pi 上 `journalctl --user -u …` 回報 no journal files；要改用
    `journalctl --user-unit rear-warning-ioradar701.service`（可加 `-f` 即時看）。
  - 停止／重啟：17:01:54 收到 SIGTERM，17:01:55 正常停止，第一段
    `service-20261125T165546.877274+0800` 跑 368.24 s，complete=true、interrupted=true，
    0 錯誤，全關與 close 成功；最後一筆 LED 是 cleanup、三色全關。17:02:13 重新啟動，
    MainPID 21269。操作員目視停止後全熄、啟動後恢復。
  - 10 分鐘切段：17:12:14 在 600.01 s 切段，UART／I²C／GPIO 沒有重開。舊段
    `service-20261125T170214.207184+0800` 有 metadata／summary，complete=true、0 錯誤；
    led 第一筆是 startup、沒有 cleanup，`led_cleanup` 三項為 false，代表「GPIO 仍在
    運作、尚未收尾」，不是失敗。該段 SAFE 11,612、WARNING 291、DANGER 12、
    SENSOR_FAULT 1，GPIO 寫入 38 次，parser 錯誤 0。每 10 分鐘約 60 MB，2 GiB 上限
    約可保留最近 5–6 小時。
  - 常用操作：暫停用 `systemctl --user stop rear-warning-ioradar701.service`；完全退回用
    `systemctl --user disable --now rear-warning-ioradar701.service`。服務執行期間
    UART 一直被占用，手動跑其他 UART 測試前要先 stop。
  - 重開機：操作員自行重開機，開機時間 17:17:06。服務於 17:17:14（開機後約 8 秒）
    由 user manager 啟動：active、MainPID 1049、NRestarts 0、enabled。`tfmini.service`
    仍為 inactive／disabled。UART／I²C 只被 PID 1049 占用，GPIO17/22/27 為 output、
    consumer=`lg`。重開前最後一段 `service-20261125T171214.200707+0800`（segment
    index 1、final=true，280.06 s）complete=true、interrupted=true、0 錯誤，全關與 GPIO
    close 成功，最後一筆 LED 為 cleanup、三色全關，表示關機時的 SIGTERM 收尾正常。
    前一次開機的 user journal 已查不到，所以這點只由 capture 證明。
  - 限制：lightdm 桌面自動登入的 session 也在 17:17:14 建立，與服務同一秒，因此
    這次重開無法證明「完全沒有登入時」也會啟動；linger=yes 的設計不依賴登入，但
    尚未在關閉自動登入的條件下實測。
  - 操作員決定：需求只是「感測器開機會自動啟動」，目前 linger=yes 加上 lightdm
    自動登入已滿足，不需要另外驗證關閉自動登入的情況。若之後關閉自動登入或
    linger，要重新確認開機自啟。
- 尚未驗證：
  1. 2 GiB 上限實際觸發刪除（約需連續運作 5–6 小時）。
  2. 斷電或非正常關機後，最後一段沒有 metadata 時的清理行為。
- 未解決風險：自啟動只解決「開機跑錯程式、占用 UART」。11-25 R2a 靜止站在約 2 m 時，
  只有 2.5% 週期有候選、燈號幾乎全程綠燈，這個偵測問題並未改善。另外，若第一個
  SIGTERM 剛好在錯誤收尾途中送達，可能中斷該次收尾；入口最後仍會呼叫
  `abort_cleanup()` 補做全關與 GPIO close，但這個時序沒有專門測試。

## 2026-11-25：新場地定點雷達控燈觀察（R1、R2 兩次；R3 未執行）

- 場地標示為「室內、比 11-19 原室內空曠的新場地」；不是 11-19 原室內，也不是空曠
  戶外，不套用 11-24 的 I/O 六輪編號，該六輪仍未執行。程式由操作員在另一終端
  以互動 Enter 啟動；沒有改 source、門檻、selector、雷達設定、開機設定或藍牙，
  沒有 stage/commit/push，原 capture 未改。
- 參數沿用 11-24 控燈命令，改為 `--duration 60 --start-delay 10`，output-root
  `hardware-results/fixed-point-20261125/`。
- 測前 15:26:52 盤點發現 `tfmini.service` active/running、PID 3286 占用
  `/dev/ttyAMA0`（可能是重開機後因 enabled 自動啟動，未確認），已停下且未 kill。
  操作員自行停止服務後，15:30:49 重盤通過：fuser exit 1 且無輸出；服務
  inactive/dead、MainPID=0、UnitFileState=enabled；GPIO17/22/27 為 input、無 consumer。
  15:55:31 測後盤點結果相同，也沒有殘留 rear_warning 程序。服務依指示不恢復，
  下次重開機會再占用 UART。
- 三個 run 的程式端都是 exit 0、`recording_complete=true`、`interrupted=false`，
  errors、cleanup_errors 為空，all_off 與 GPIO close 皆成功，parser checksum/sync/trailing
  皆為 0：
  - R1 背景，`R1/live-20261125T153310.217600+0800`：觀測區無人、無物體移動；
    GPIO 寫入 9 次，程式判定 SAFE 99.6%。操作員採集中沒注意燈號，目視未驗證；
    結束後終端顯示全關，目視六燈全熄。
  - R2a 靜止，`R2/live-20261125T154148.311337+0800`：量測約 2 m 站定，無他人移動；
    目視全程綠燈，操作員主觀「雷達沒量到」。1.5–3.5 m 候選只出現在 30/1,192 週期，
    最長連續 16 週期；WARNING 佔 2.1%，GPIO 寫入 5 次。
  - R2b 晃動，`R2/live-20261125T155110.731924+0800`：設計為重跑 R2，但操作員誤用
    R2 命令，因此不在 `R2-retry/`，與 R2a 並存、無覆寫。站位比 2 m 近，未量測。
    操作員回報靜止偵測不到，需大幅晃動才有燈號變化，**標為非靜止，不當作定點資料**。
    1.5–3.5 m 候選出現在 645/1,192 週期；WARNING 52.6%、DANGER 1.7%，GPIO 寫入 30 次。
  - R3 近距離：操作員看過 R2 結果後決定停止實測，未執行。
- 資料支持：
  - R1 每週期約 6–7 個候選，固定出現在約 5–5.5、7、8、12、19–19.5、23 m；
    2.5–3.5 m 候選為 0，不支持本場地有 11-19 那種約 2.8 m 固定帶。
  - <3.5 m 候選幾乎都帶 Vx≠0（R2a 30/30、R2b 625/677）。≥3.5 m 候選在 R1 是
    0/7,676，在 R2a 是 14/7,484。這與「靜止站立偵測不到、晃動才變燈」相符。
  - 兩次 R2 都沒有出現「1.5–3.5 m 有候選卻被更近候選搶走選擇」的情況。
  - 同 ID ≥0.2 m 單步：R1 0/1,168、R2a 3/1,142、R2b 48/1,144；11-19 參考 run 為 162/3,429。
  - 同 ID 連續選中段內距離 std（ID 改變或無選中週期即斷段；只算 ≥2 週期的段）：
    中位數 R1 0.024 m、R2a 0.019 m、R2b 0.046 m、11-19 0.074 m；std ≥0.2 m 的段
    R1 0/20、R2a 1/50、R2b 7/46、11-19 40/142。靜止的 R1/R2a 段內抖動多遠小於
    0.2 m 遲滯帶；不分段的各 ID std（R2a 中位 1.13 m）主要反映 ID 被重新指派到
    不同距離，不代表同一目標抖動。
  - selected 週期中 ≥4 候選的比例：新場地三輪皆 ≥99%，11-19 原室內為 25.6%。
    兩者場地、時長、動作不同，只陳述差異，不歸因於場地。
- 資料不支持：
  - 斷定雷達會過濾靜止目標，或歸因於韌體、擺放、朝向。
  - 認定近距離候選就是操作員本人；ID 不是物理身分，也沒有空間真值。
  - 判斷 R1 各固定帶對應的實體物件。
- 唯讀分析位於 `hardware-results/fixed-point-20261125/analysis/`，內含
  `analyze_fixed_point.py`、`results.json`、`report.md`。分析前後四個 run 的輸入
  SHA-256 一致；11-19 參考 run 重算結果與既有紀錄相符（140 switches、100 次變燈、
  ≥2 候選 3,158/3,570、≥4 候選 915、同 ID 最大單步 0.839 m）。`hardware-results/`
  已被 `.gitignore` 排除。
- 待決定：是否在同一擺放補跑 R3 靜止／晃動、R2 先量好站位再重跑，或先核對
  雷達擺放／朝向與靜止目標行為。far/release 事件仍延期。

## 2026-11-24：ioRadar701 室內／空曠場地對照測試移機前暫停點

- 目的為比較室內（I）與較空曠場地（O）各至少三輪、每輪 180 秒的候選數、
  最近目標 ID 切換、距離跳動、LED 變化及小於 1 秒的變燈間隔。建議順序
  `I1 → O1 → O2 → I2 → I3 → O3`；實際搬運受限時保留真實順序。11/19 數據只作
  歷史參考，`far`／`release` 事件繼續延期。
- 本輪開始前核對現行設定仍為 `/dev/ttyAMA0`、230400/8N1 receive-only、BNO055
  I²C bus 1/address 0x29，以及 shared_3 LED：紅 BCM17/Pin11、黃 BCM27/Pin13、
  綠 BCM22/Pin15，active-high，同色 A/B 共控且每顆各串 220 Ω。距離規則仍為
  DANGER `<1.5 m`、WARNING `1.5–3.0 m`、SAFE `>3.0–12 m`，解除門檻
  `1.7/3.2 m`、TTL 1 秒；selector、門檻與遲滯均未修改。
- 前置盤點曾發現 `tfmini.service` active/running、PID 3247 占用 UART；操作員已於
  Pi 終端停止服務。隨後核對為 `inactive/dead`、MainPID=0、UnitFileState=enabled，
  UART／I²C 的 `fuser` 無 owner，GPIO17/22/27 均為無 consumer 的 input。本輪不改
  enabled／開機設定，結束後也不自動恢復服務；移機後正式開始前仍須重新核對 owner。
- `ioradar701_led` 新增 opt-in `--start-delay SECONDS`。按硬體確認 Enter 後先倒數，
  倒數期間不建立 GPIO、UART 或 I²C owner；倒數完成才開始完整 `--duration 180`
  採集。預設仍為 0 秒，I1 使用 5 秒。負數、NaN、infinity 會在硬體 I/O 前拒絕；
  倒數期間 Ctrl+C 回傳 130 且保持零硬體 I/O。受影響離線回歸實跑 41 passed。
- Codex 曾把 I1 程式啟動到 Enter 提示，但因操作員改為自行貼命令，已在 Enter 前
  以 Ctrl+C 取消（exit 130）；該次沒有取得 GPIO/UART/I²C，也沒有有效 capture。
  截至此暫停點，六輪實測均尚未執行，不得標為 PASS。
- I1 建議距離從 ioRadar701 感測面沿車尾正後方中心線量測：遠站位 5 m、近站位
  1 m。按 Enter 後前 5 秒只供操作員走位；空窗結束定義為採集 `t=0`。動作為：
  0–30 秒目標在觀測路線外；30–60 秒於 5 m 站定；60–120 秒在 5 m 與 1 m 間
  以相同步調往返三次；120–150 秒於 5 m 站定；150–180 秒離開觀測路線並保留
  固定背景。若場地只能使用其他距離，須記下實際值，六輪保持相同。

I1 待操作員在裝置上執行的命令（硬體仍由互動 Enter 門檻保護）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -u \
  -m rear_warning.cli.ioradar701_led \
  --port /dev/ttyAMA0 --baudrate 230400 --uart-timeout 0.02 \
  --i2c-bus 1 --bno055-address 0x29 \
  --motion-period 0.1 --maximum-motion-age 0.2 \
  --duration 180 --stale-after 1.0 --refresh-hz 5 \
  --gpio-chip 0 \
  --output-root hardware-results/site-comparison-20261124/I1 \
  --start-delay 5 \
  --confirm-hardware --enable-led --plain
```

操作員完成當日工作後，至少回傳：實際輪次與場地、每輪 `capture_directory`、開始／
結束時間、實際遠近距離、各段實際動作時間與延誤、旁人／大型反射物異常、六燈目視、
終端最終 summary、`recording_complete`／errors／cleanup，以及結束後 UART／I²C／GPIO／
`tfmini.service` owner 快照。未實際輸入的事件不得事後補寫；原 capture 與 WIP 保留。

## 2026-11-19：修正後雷達控燈實機跳動唯讀分析

- 本次只讀分析
  `hardware-results/radar-led-selector-fix-20261119/live-20261119T020805.728038+0800`，
  沒有重跑硬體、改門檻／selector、啟停服務或操作藍牙。原 capture/events/LED
  未改，far/release 繼續延期。
- run 正常到期 180.161217 s，complete=true、interrupted=false、errors/cleanup_errors
  空。保存 161,856 raw bytes、13,488 decoded、1,646 BNO、3,572 SHADOW、3,574 LED；
  parser checksum/sync/trailing 與 radar time discontinuity 全 0。LED cleanup 全關與 GPIO
  close 明記成功。
- `events.jsonl` 只有啟動期 invalid BNO measurement，沒有 operator event。
  `nearest_overlap`、`danger`、`far`、`release` 均不存在且未補寫，無法將特定
  人員動作與跳動直接對齊。
- 3,569 applied＋1 coalesced 的所有 3,570 個 selected assessments 都等於
  `(distance_m, target_id)` 的當期最小者，mismatch 0；2 個 NO_TARGETS 均沒有殘留
  selected target。selector 修正已在真實 live 資料中確認生效。
- 相鄰 applied cycles 有 140 次 ID switch：60 次前 selected ID 消失，80 次原 ID
  仍在但另一 ID 變得更近。不同 ID 切換 `>=1 m` 53 次、`>=2 m` 12 次；
  同 ID 最大單步 0.839 m、`>=1 m` 為 0，但同 ID 跨現有門檻／遲滯造成
  39 次 stabilized state 變化。
- 140 次 switch 都依原設計 reset。反事實比較顯示只有 6 次會因 reset 而與
  延續前 target 遲滯得到不同 state；這 6 次均變燈，但不是 100 次
  assessment pattern 改變的主要來源。
- LED/SHADOW 3,572 筆 timestamp 全對齊，逐筆重算 pattern 與 write flag 的
  mismatch 均 0。100 次 assessment 變燈由 56 次 ID-switch state change、39 次同 ID
  跨邊界、2 次 NO_TARGETS 全關、2 次重新取得及 1 次初始顯示組成。變燈
  間隔中位數 0.857 s，55/99 小於 1 s，支持操作員的反覆變化回報。
- 3,158/3,570 selected assessments 有 >=2 candidates，915 個有 >=4，最多 7。
  重複候選距離群約在 2.8、4.6–4.7、6.4–6.5、7.5–7.6 m；1,816/3,569 applied
  cycles 有 >=6 m candidate。這支持觀測區有多個反射來源，與家具／牆壁
  假說相容，但因無 stage events、空曠場對照、空間真值且 ID 非物理身分，
  家具／牆壁成因仍是未驗證。
- 當前主機 owner 快照於 `2026-11-19T02:17:53+08:00` 取得：UART/I²C 無
  fuser owner；GPIO17/22/27 均 input 且無 consumer；`tfmini.service` inactive/dead、
  MainPID=0。這是晚於 run 結束約 6:47 的當前快照，不倒補成結束同一瞬間。
- 已建立「同程式、同接線、同人員／目標／動作，只搬到較空曠場地」的未執行
  對照方案；建議室內／空曠場各至少 3 輪，比較 candidate 數、ID switch、距離
  跳動、短間隔變燈與 cleanup。本次不執行、不先改軟體。
- 完整雜湊、6 次 reset 影響、LED 對齊方法、已證實／未知及場地對照方案見
  `docs/ioRadar701_最近目標實機跳動分析_2026-11-19.md`。

## 2026-11-19：ioRadar701 selector 最近目標修正與零 GPIO replay

- 修正已限定在既有 `RadarTargetSelector`：每個完整週期都依
  `(distance_m, target_id)` 選最小者，規則名改為
  `minimum-hypot-then-id`；移除舊 ID 仍在就無條件優先的行為。parser、候選
  週期、幾何距離、距離門檻、遲滯、TTL、LED policy 與 BNO 旁路未改。
- ID 切換仍走原 target-change reset，清 adapter 與重建 stabilizer 後立即以同
  週期新目標判斷。Regression 已覆蓋舊 ID 4 m／新 ID 1 m 並存時當週期
  選 1 m、reset、DANGER 與紅燈意圖，並覆蓋同距離 ID tie-break、輸入順序、
  壞 checksum 候選、零目標、不完整週期與 reset。
- 測試實跑：聚焦 30 passed；受影響回歸 434 passed；full suite 1,456 passed＋
  1 個 sandbox loopback PermissionError，該唯一測試在允許 localhost socket 的隔離
  環境重跑 1 passed。
- 新的零 GPIO replay 保存於
  `shadow-results/selector-fix-20261119/replay-20261119T014540.173481+0800`。metadata
  為 `mode=REPLAY`、`shadow=true`、`gpio_enabled=false`、無 `led.jsonl`，並明記
  `selector_rule=minimum-hypot-then-id`、complete=true、errors=[]。計數仍是
  147,612 raw bytes、12,301 decoded、1,648 BNO、3,573 shadow、3,547 applied 與 1 coalesced。
- 逐 key 對齊證明 3,573/3,573 candidate payload 與 fusion-update 序列未變。原本
  951 個 non-nearest selections 已降為 0；原 272 個「選中 3.5–5 m 但同期有
  <1.5 m candidate」的週期，272/272 都改選最近目標並在同週期得到
  candidate/stabilized DANGER。來源 raw/events/led 雜湊未變。
- 不利交易也已量化：ID switch 34 → 109；`>=1 m` 不同-ID 跳動 22 → 70；
  依 SHADOW 狀態推算的 LED pattern 切換 58 → 110；fusion warmup 1,210 →
  1,520，live-selected 2,335 → 2,025。修正後的 `>=1 m` 跳動仍全是不同
  ID，同 ID 單步 `>=1 m` 仍為 0。這是 candidate set/ID 變動被最近目標規則
  直接反映，實體燈穩定性仍待現地驗證。
- 本階段沒有開 UART/I²C/GPIO、操作服務或藍牙，沒有改門檻、原 capture
  或事件，沒有 stage/commit/push。far/release 繼續延期；離線 DANGER/紅燈
  意圖不等於兩顆實體紅燈已通過。
- 完整命令、測試、replay SHA、逐週期統計、剩餘風險與分項判定見
  `docs/ioRadar701_距離控燈目標選擇分析_2026-11-19.md`。

## 2026-11-19：ioRadar701 距離控燈目標選擇異常唯讀分析

- 本次只唯讀核對
  `hardware-results/radar-led-20261119/live-20261119T011829.633262+0800`；沒有重跑
  硬體、操作服務／藍牙、改門檻／遲滯／selector，也沒有改原始 capture。
- run 正常到期 180.178745 秒，`recording_complete=true`、`interrupted=false`，
  runtime/cleanup error 皆 0。保存 147,612 raw bytes、12,301 decoded frames、
  1,648 BNO、3,573 SHADOW 及 3,575 LED records；parser checksum/sync discard/trailing 皆 0。
  GPIO 明記全關與 close 成功，UART/BNO close 沒有 exception；因無結束後 owner 快照，
  kernel owner postflight 單獨保留未驗證。
- `events.jsonl` 只有啟動期 `invalid_motion` 與一筆 operator `warning`（elapsed
  28.519955 s）。`danger`、`far`、`release` 均缺少且未補寫。warning 前後 ±3 s
  的 118 筆資料全是 ID1、約 2.84–2.86 m、stabilized WARNING 及只亮黃燈，
  與操作員目視相符。紅／綠實體燈與 release 本輪仍未驗證。
- 所有相鄰距離差 >=1 m 的 22 次跳動都是不同 ID；>=2 m 的 7 次也全是
  舊 ID 消失後的 target switch，沒有同 ID >=1 m 單步跳動。
- 關鍵異常為現行 `hold-current-else-minimum-hypot-then-id` selector：3,547 個
  applied complete cycles 中，951 個未選最近 candidate；其中 272 個週期選中
  3.5–5.0 m，但同期仍有 <1.5 m 合法 candidate。這 272 筆全是
  `held current target ID`、`target_changed=false`、stabilized SAFE；較近目標並未因
  parser、週期完整性或有效範圍被排除。
- 最小修正建議是只調整既有 selector：保留 `(distance_m, target_id)` 規則，
  不再讓仍存在的遠 ID 無條件壓過更近 candidate；不在 LED 層另建第二套
  policy，不改距離門檻與遲滯。此修正尚未實作，需下一步以合成 cycles 與
  本 capture 唯讀 replay 驗證 target/LED 切換量。
- 完整事件原文、候選 ID 統計、切換與 reset、12 段遠選中／近候選區間、
  LED 記錄、SHA-256 與分項判定見
  `docs/ioRadar701_距離控燈目標選擇分析_2026-11-19.md`。

## 2026-11-18：機車模型裝配後 LED／ioRadar701／BNO055／共同運行檢查

- 本輪藍牙完全排除；保留全部 dirty WIP、capture、profile 與非最終紀錄，沒有
  stage／commit／push、sudo、開機設定變更或警示門檻變更。
- 六顆 LED 現地為三路同色共控：BCM17/27/22 active-high，各色 A/B 兩顆共用一個
  GPIO controller，每顆各自串聯 220 Ω。新增 confirmation-gated
  `tools/assembled_led_test.py` 與明確 TOML 接線表；正常／錯誤／Ctrl+C 都嘗試全關
  與 close。T1 六顆逐顆目視及無非預期亮燈均 PASS，cleanup 全成功。
- T2 ioRadar701 20.0168 秒：15,753 raw bytes、397 status、915 target、ID 0–4、
  checksum 0、trailing 0；起始 sync discard 9 bytes 後持續正常，週期品質全完整。
  目標距離／Vx 有明顯反應；通訊與目標反應 PASS，但不宣稱 ID 真值、車速或精度。
- BNO055 API 最小增加 raw acceleration 與 angular velocity (°/s) 的硬體無關解碼，
  診斷加入 guided stage label；不改 profile／offset／axis map。T3 最終 275 samples、
  30.023 秒、9.160 Hz，完整 identity `A0/FB/32/0F`、NDOF `0C/05/00`，所有 read／
  runtime error 為 0、cleanup 成功。中段動作反應明顯，停止後最後約 7 秒回穩；
  calibration `(0,3,0,0)` 不當作故障或完整校正。
- T4 A 是唯一 UART／I²C SHADOW reader，120.0609 秒完整保存 106,068 raw bytes、
  8,839 decoded、1,099 BNO、2,381 SHADOW；parser/runtime/cleanup error 皆 0，
  `gpio_enabled=false` 全程不控制實體警示。B 只跑 shared_3 LED 診斷，六燈再次
  目視 PASS 且全關／close 成功。
- B 的真實 LED 區間為 A 開始後約 26.83–50.13 秒。該 23.31 秒內有 213 BNO、
  1,813 radar decoded、463 SHADOW；最大更新間隔分別約 0.113/0.070/0.070 秒，
  無凍結或 I/O error，因此 LED 切換期間共同運行 PASS。
- T4 `events.jsonl` 只有合併的 `static_start:event led_test_start`；
  `led_test_end`、`motion_start`、`static_end` 缺失，未事後補寫。80／100 秒人員
  階段真值標為未驗證，不影響以 LED 真實牆鐘直接核對的共同運行證據。
- 測後 `fuser -v /dev/ttyAMA0 /dev/i2c-1` 無輸出；`tfmini.service` 維持
  inactive/dead、MainPID=0、UnitFileState=enabled，沒有自動恢復。T1/T4 工具均回報
  GPIO all-off/close 成功；操作員再以 libgpiod 2.2.1 正確語法
  `gpioinfo -c gpiochip0 17 22 27` 核對三路均為 input 且沒有 consumer，GPIO
  postflight 資源釋放 PASS。
- 本輪 BNO targeted 728 passed、T4 前 targeted 87 passed；full suite 在 sandbox 為
  1,431 passed＋1 個 loopback socket PermissionError，該唯一測試在允許本機 socket
  的環境隔離重跑 1 passed。`py_compile`、`git diff --check` 通過。
- 完整接線表、各輪路徑、SHA、T4 LED overlap 計數、非最終紀錄、資源收尾與
  PASS／未驗證矩陣見 `docs/機車模型裝配後全硬體功能檢查_2026-11-18.md`。

## 2026-11-08：開機 MOTO 自連與中文 SHADOW 儀表板

- A 程式已完成並安裝：`topics` 原本 linger=yes；新增單一
  `rear-warning-moto-autoconnect.service`（30 秒一輪、每輪最多 20 秒）與 `topics` 專用
  WirePlumber headless seat override。服務 enabled/active、`NRestarts=0`，當前動態核對
  connected 且恰一個 MOTO `Audio/Sink`，`Discovering=no`。無掃描／配對／播放，語音仍
  `enabled=false`。
- A 兩個功能情境已通過：耳機先開時，linger user manager 於開機約 6.825 秒啟動，
  `default.target` wanted 的自連 unit 於 8.745 秒啟動，9.152 秒記錄 connected 且
  唯一 sink；第一個 SSH 為 31.113 秒，不是 SSH 觸發。Pi 先開、耳機後開時，
  服務 log 從 789.153 秒 disconnected 轉為 819.154 秒 `audio_ready`，差 30.001 秒，
  通過 90 秒界線。全程 `Discovering=no`、`NRestarts=0`、無手動 connect／播放。本次
  boot 已有 Wayland session，因此純無桌面 boot 實景仍沒有另做，不虛報該額外情境。
- B 離線程式已完成：新增 loopback-only `rear_warning.dashboard`，只讀現行 SHADOW JSONL，
  2 Hz HTTP polling、有界 120 點歷程、run 切換清理、斷流／過期／停止／零目標語意與本機
  HTML/CSS/canvas，不開第二個 UART／I²C reader。SHADOW 只增加 candidates 保存與
  motion/shadow JSONL 逐行可見，門檻、取樣、parser 不變。
- B 真實畫面已通過：操作員手動停止已知 `tfmini.service`並確認 UART／I²C 無 owner，
  儀表板只綁 `127.0.0.1:8765`，再由互動門檻 Enter 啟動 60 秒 ioRadar701／BNO055
  SHADOW。Pi 現地時鐘產生的 run 為 `live-20261101T123449.630960+0800`；它與
  2026-11-08 任務日期不同，故保留原名並明記時鐘差異。
- 操作員確認採集中雷達／BNO 數值與圖表持續更新，採集後網頁顯示來源已停止而非
  即時。API 實測由 live 轉 stopped，stopped 時 radar/BNO age=null。capture 為 30,036 raw bytes、
  2,503 decoded frames、552 BNO、1,192 SHADOW records；parser errors、runtime errors、cleanup errors 全 0，
  `recording_complete=true`、`interrupted=false`。結束後兩裝置無 owner，8765 listener 已停止。
- 依操作員明確要求，`tfmini.service` 保持 inactive，本輪不自動恢復；語音仍
  `enabled=false`，無播放、GPIO／LED 或實體警示。
- 本輪 fake／loopback 目標測試 11 passed in 0.69s，另有 py_compile、CLI help 與離線
  HTTP/API 核對。沒有完整歷史回歸、LED／BNO／雷達快測、斷線驗收、道路、播放、
  stage／commit／push。安裝、查詢、停用、還原、Web／SSH 轉送與待驗命令見
  `docs/開機藍牙自連與感測儀表板_2026-11-08.md`。

## 2026-11-08：MOTO A2 Plus 首次藍牙語音播放（首句未通過後停止）

- 操作員確認後，新增獨立 `tools/voice_playback_experiment.py`。工具從私有設定讀取 MOTO
  身分與明確 target，沿用既有 backend 確認 BlueZ paired／connected 與設定指定的唯一
  PipeWire `Audio/Sink`；沒有寫死舊節點編號。
- 播放前 PipeWire、pipewire-pulse、WirePlumber user services 皆 active，MOTO sink 唯一且可用。
  播放使用明確 target、`node.dont-fallback=true`、`node.dont-reconnect=true`、
  `node.dont-move=true` 與單次串流 `--volume=0.25`；沒有改預設輸出或全域音量。
- 第 1 句 `rear_distance_attention.wav`（「請留意後方距離」）的 `pw-play` 退出成功；操作員確認
  確實從 MOTO A2 Plus 聽到，但內容不正確清楚／有漏字或截斷、音量太小，並且有卡頓。
  結果為未通過，不能因程式退出成功就記為可聽驗證通過。
- 依停止條件未重播、未調高音量、未切換裝置。第 2 句 `rear_distance_too_close.wav` 與第 3 句
  `sensor_fault.wav` 都沒有播放，聽感全數未驗證。工具以失敗狀態退出，沒有殘留 `pw-play`。
- 本輪只執行新工具的語法編譯與 `--help` 檢查，沒有完整回歸、斷線測試、道路實驗、感測器
  重測或正式整合。`config/voice-alert.local.toml` 全程保持 `enabled=false`，沒有操作配對、預設
  輸出、全域音量、開機設定或服務。

## 2026-11-08：MOTO A2 Plus 中文語音素材已放入（未試播）

- 已將 `MOTO_A2_Plus_中文語音素材_2026-11-07.zip` 先解壓至專案外暫存目錄，依包內
  `PROVENANCE.json` 核對檔名、大小與 SHA-256。三檔全部相符，且專案原本沒有同名
  WAV，因此無衝突、無覆寫。
- 已放入 `assets/voice/rear_distance_attention.wav`（191748 bytes，SHA-256
  `f962b3c10e3e0c37666b00b2552e368ea20e4841b98e8475d4e62bbb286682e7`）、
  `assets/voice/rear_distance_too_close.wav`（232400 bytes，SHA-256
  `defb13d689d2429f1a63d32f99cabef75b47fb54259e794c856b54350ec4eaf7`）與
  `assets/voice/sensor_fault.wav`（166714 bytes，SHA-256
  `0b60db9f7df4fc6ff608bba105850e7ab9a304f404cfcda2b2bba75855292016`）。
- 素材來源資訊已原樣保存於 `assets/voice/PROVENANCE.json`；素材包 README 沒有覆寫現地文件。
- `assets/voice/prompts.toml` 原有 schema、中文文字、事件與三個檔名對映正確；本次只將
  三個 `present` 狀態更新為 `true`，沒有修改 TTL、冷卻、優先權或警示邏輯。
- 被 `.gitignore` 排除的 `config/voice-alert.local.toml` 私有設定未改，並仍維持
  `enabled=false`；沒有新增真實 MAC 或含 MAC 的節點名稱至文件。
- 本輪依任務邊界沒有執行單元測試、編譯檢查、模擬、dry-run、試聽或播放，也沒有
  啟用語音、操作藍牙／音量／服務、接入 SHADOW 或正式偵測流程。檔案就位不等於
  語音已驗證可聽。

## 2026-11-01：MOTO A2 Plus 藍牙語音模組（已實作、未試播）

### 操作員回報／既有邊界

- 操作員回報重新接線後 ioRadar701 收包、BNO055 身分碼及三顆 LED 基本快測已通過；
  本輪沒有重跑、沒有改線。現在實體距離感測器是 ioRadar701，沒有 TFMini 硬體。
- 保留現有 SHADOW／live、GPIO、距離與融合門檻、BNO055 校正、capture、profile 及全部
  dirty WIP。沒有處理舊 `tfmini.service`，沒有新增開機服務，沒有 stage／commit／push。

### 已實作

- 新增獨立 `rear_warning.voice` package：
  - `models.py` 明分提醒事件、提交結果、非同步 delivery report、BlueZ paired／connected
    與 PipeWire audio-output availability。
  - `dispatcher.py` 預設 disabled，建立物件零外部 I/O，必須明確 `start()`；感測／融合
    caller 只做短鎖 queue 操作。支援 dedup、8 秒預設 cooldown、3 秒 TTL、4 筆 bounded
    queue、單一 worker 與優先權替換。路由失效會清除待播與該批 cooldown，恢復後只接受
    新事件，不補播舊事件。
  - `backend.py` 固定完整 MOTO MAC、BlueZ Name 及唯一 PipeWire Bluetooth
    `Audio/Sink`；每次播放前分別核對連線與 audio node，使用
    `pw-play --target=<explicit>`，並設定 `node.dont-fallback=true`、
    `node.dont-reconnect=true`、`node.dont-move=true`。不允許 `auto`，因此不會默默
    改播 HDMI／analog 或在 target 失效後重接其他 node。
  - `config.py` 使用標準庫 TOML loader；disabled 設定可保留 placeholder，enabled 時
    拒絕未填 MAC／target。import、load、build 都不掃描、配對、連線或播放。
- 事件映射固定為：WARNING→「請留意後方距離」、DANGER→「注意後方，距離過近」、
  SENSOR_FAULT→「感測資料異常」；SAFE 不播，UNCERTAIN／未知／無效值拒絕且不產生
  安全提示。未接入現有 SHADOW，語音仍為 opt-in output。
- 新增 `config/voice-alert.example.toml`、WirePlumber 0.5 optional fragment、
  `assets/voice/prompts.toml` 與中文文件 `docs/MOTO_A2_Plus_藍牙語音.md`。

### 現地服務／音訊盤點

- 目前 shell 使用者為 `topics` uid 1000；授權後以 logind 確認 `topics` 同時擁有
  active Wayland seat0 session。SSH session 雖不同，但共用 uid 1000 user audio
  services；不需先停用 WirePlumber seat monitoring。
- 已安裝 BlueZ 5.82、PipeWire 1.4.2、WirePlumber 0.5.8；存在 `bluetoothctl`、`wpctl`、
  `pw-play`、`pw-dump`、`pw-cli`。PulseAudio／pulseaudio-utils 未安裝，無 `pactl`／
  `paplay`，因此沒有新增 PulseAudio 相依。
- 授權後確認 BlueZ system service active/running；PipeWire、pipewire-pulse、
  WirePlumber 均以 `topics` user service active/running。adapter
  指定 adapter powered／pairable、非 discoverable、`Discovering: no`。
- 初始 BlueZ 已知 device 清單為空、PipeWire 只有 `auto_null`。操作員切入配對模式後，
  兩輪各不超過 30 秒 discovery 唯一辨識 MOTO A2 Plus；
  第一輪只辨識，第二輪在 transient object 存在時完成配對。兩輪最後都確認
  `Discovering: no`，其他掃描裝置未操作。
- 只對該 MAC 完成 pair／trust／connect。最終 BlueZ：Name／Alias 均為
  `id221 MOTO A2 Plus`，Paired／Bonded／Trusted／Connected 均 yes，配對事件
  ServicesResolved=yes，Audio Sink／A2DP UUID 存在。文件收尾前的唯讀快照仍為
  Connected=yes、battery 90%、`Discovering: no`；未試播。
- PipeWire 建立指定 MOTO 的 Bluetooth device 與唯一 sink（Audio/Sink、bluez5、A2DP sink、
  SBC）。WirePlumber 自動顯示它為 `*`、volume 0.80；本輪未執行 set-default 或改音量。
- 現地 WirePlumber 0.5.8 scripts 已直接核對支援 `node.dont-fallback`、
  `node.dont-reconnect`、`node.dont-move`；PipeWire 1.4.2 local man page 確認
  `pw-play --properties=<JSON>`。不是只依賴 `--target`。

### 已設定／缺項／未驗證

- repo 內範例與實際 local config 都維持 `enabled=false`。實際 MAC、Name、target、
  node serial／profile／codec 與觀察狀態保存在被 `.gitignore` 排除的
  `config/voice-alert.local.toml`；未安裝 WirePlumber fragment、未改全域音量或手動改
  預設 sink。audio session owner 已確認為 `topics`。
- 三個實體 WAV 均缺少：`rear_distance_attention.wav`、
  `rear_distance_too_close.wav`、`sensor_fault.wav`。只交付文字／檔名 manifest，不建立
  靜音檔或假成功。現地沒有 Piper／model、espeak、Festival、Flite、Pico TTS、Speech
  Dispatcher、RHVoice、Coqui／其他 Python TTS 或既有音檔，因此沒有可用離線中文來源。
- 任務明令本輪不執行測試、模擬、回放、dry-run、編譯檢查或試播；本輪只做人工靜態
  審閱。已檢查模組邊界、預設關閉、明確 target、TTL／queue／disconnect 語意及外部
  command 只在 worker backend 路徑；但語法、型別、concurrency、播放、音檔、音量、
  延遲與可聽度仍未驗證，不能宣稱語音可聽見。Bluetooth connected 與 Audio/Sink 已
  建立也不能取代實際播放驗證。
- 剩餘主要缺項是三個真實中文 WAV 與未來另案 runtime／試播驗證；本輪仍未接入
  SHADOW。完整命令、狀態、設定條件與限制見中文文件。

## 2026-11-01：重新接線後功能快測工具（僅完成離線驗證）

### 操作員回報（不是本輪感測器實測結果）

- Pi 5 拆殼重新接線後曾只有本體紅燈且電源鈕無反應；拔除全部外接杜邦線後可開機，
  分組逐步接回後也已正常開機。原始原因仍未定位，目前保留能開機的接法。
- 上述只證明操作員目前可開機；不代表 LED、BNO055、ioRadar701、距離精度或完整警示
  流程已通過本次驗證。本輪未重做開機排查、未改線或供電。

### 本輪實作與離線檢查

- 新增 `tools/wiring_quick_test.py`，提供預設 `all` 及 `led`／`bno`／`radar` 模式；
  `all` 固定依 LED、BNO055、ioRadar701 執行。程式先列出接線、裝置與通訊設定，按
  Enter 後才進行 owner preflight 與 lazy-load 硬體 backend；EOF／Enter 前 Ctrl+C
  不會開硬體。
- LED 使用 active-high BCM17／27／22，依 sysfs 的 RP1／pinctrl label 選 lgpio chip，
  紅黃綠各亮 1 秒後全關，再要求操作員輸入目視確認。正常、Ctrl+C、部分初始化與
  寫入失敗都會盡力全關並 close；人工未確認時結果為 `INCONCLUSIVE`。無法從 sysfs
  可靠選 chip 時不猜測、不開 GPIO，會要求操作員查明後明確使用 `--gpio-chip N`。
- BNO055 只以 SMBus 1 讀 register `0x00`：先試 `0x29`，必要時試 `0x28`，只有
  `CHIP_ID=0xA0` 才通過。不初始化、不切模式、不讀寫校正資料，finally 關 bus。
- ioRadar701 固定 `/dev/ttyAMA0`、230400／8N1／無流控、只收 5 秒，不呼叫 UART
  write。沿用硬體無關 `IoRadar701StreamParser.feed(bytes)`，支援任意分段、半幀、
  多幀、checksum failure 與重新同步；顯示合法 status／target、checksum、discard、
  trailing 計數，finally 關 serial。
- UART／I²C 開啟前以 `fuser` 檢查可見 owner。只有 `systemctl show` 的
  `tfmini.service MainPID` 是唯一 owner 時才正常 stop，全部測試／中止後再 start；
  未知或混合 owner 只顯示 PID／程序名並拒絕該項，不 kill、不刪鎖檔、不改服務開機
  設定。各項失敗不阻止其他獨立項目繼續。
- 已閱讀 `AGENTS.md`、本交接檔及 ioRadar／BNO／LED 相關現地文件；
  `docs/CODEX_PROJECT_TRANSFER_TASK.md` 現地仍缺少，沒有等待該檔或假稱已同步。
- 本輪實際離線執行並通過：兩個檔案 `py_compile`、CLI `--help`（exit 0），以及
  `tests/tools/test_wiring_quick_test.py`、既有 ioRadar parser、gpiozero adapter、SMBus
  adapter 共 **50 passed in 0.17s**。所有測試使用 fake／純 bytes，沒有開 UART、
  I²C、GPIO、藍牙或操作 systemd；歷史實機數字未當成本次結果。

### 操作員實機命令（本輪尚未執行）

先進入專案：

```bash
cd /home/topics/Projects/Smart-Rear-Collision-Warning-System
```

全部／三個單項：

```bash
.venv/bin/python tools/wiring_quick_test.py all
.venv/bin/python tools/wiring_quick_test.py led
.venv/bin/python tools/wiring_quick_test.py bno
.venv/bin/python tools/wiring_quick_test.py radar
```

每條命令會先顯示設定並停在 Enter。若列出的 GPIO chip、接線、供電、共地、owner 或
服務狀態與現地不同，先停止、不改線；已知正確的非自動 GPIO chip 可明確加
`--gpio-chip N`，畫面會同時列出 override 與自動偵測差異。ioRadar 接法維持藍板原
ESP32 插槽 IO16 → Pi 實體 Pin 10／GPIO15 RX、既有獨立供電與共地；Pi Pin 8 不接，
不改回藍板 P1 Pin 10。

預期可見：LED 逐燈訊息與人工確認；BNO055 的 address／CHIP_ID；ioRadar 的 bytes、
合法 status／target 幀、checksum failure、sync discard、trailing；最後每項
`PASS`／`FAIL`／`INCONCLUSIVE` 摘要。LED 必須由操作員目視；BNO `0xA0` 只證明基本
I²C／身分通訊；合法雷達幀不證明距離精度或完整警示驗收，零 target 也不直接證明
接線失敗。四條實機命令均待操作員依現地安全程序按 Enter 執行與回填觀察。

## 2026-11-01：SHADOW 操作員事件標記與 BNO profile 唯讀盤點

- SHADOW 原先沒有執行中操作員事件輸入；現已最小加入同程序、非阻塞 stdin
  `:event <detail>`。標記以 `kind=operator`、原始 detail 及讀到完整輸入行後立即取得的
  host-monotonic `timestamp_seconds` 寫入並 flush `events.jsonl`。不新增 thread／process、
  UART reader 或 GPIO；採樣、batch/fusion core 與 finally cleanup 路徑不變。
- Replay 會按來源時間線保留 operator events；同時相容 SHADOW 的
  `timestamp_seconds` 與 standalone ioRadar 舊事件的 `elapsed_seconds`。五輪舊 SHADOW
  已重播到 `shadow-results/event-marker-compat-20261101/`，每輪四個 JSONL 逐 byte 相同，
  summary/parser/counts 一致，全部 `recording_complete=true`。
- 現地 Git 目錄沒有 calibration profile JSON。歷史文件記錄 2026-09-12 profile 位於
  專案外 `/home/topics/.local/state/rear-warning/bno055/profile.json`；依現地規範本輪未越出
  Git 專案讀取，故不宣稱該外部檔目前仍存在或仍相容，亦未 restore/export。
- 現行 SHADOW CLI 沒有 profile 參數或 store/restore 呼叫；實際流程是
  `read_identity()` 後直接 `initialize_ndof()`。五輪舊 metadata 只有 NDOF
  `0x0c/0x05/0x00`，沒有 profile 欄。新 live metadata 會明記
  `bno055_calibration_profile.loaded=false`，終端啟動計畫也會顯示未載入。
- 語意已分清：現行 data quality 只檢查 mode/status/error、finite、quaternion norm 與
  gravity norm；full calibration 是 CALIB_STAT 四項皆 level 3；profile compatibility
  另需 schema/hash/sensor label、identity/firmware、axis map、22-byte readback、NDOF
  readiness 與 quality。BNO055 無可用唯一序號，以上仍不證明同一實體或外部角度精度。
- 本輪 hardware-free regression 實跑 1025 passed in 3.31s；另完成五輪真實保存資料
  replay 相容核對、CLI help 與靜態檢查。沒有開 UART/I²C/GPIO、沒有改 profile／品質／
  分類門檻，沒有 stage/commit/push。
- 操作命令、事件範例、replay 路徑、完成與待驗證項：
  `docs/進度斷點_2026-11-01_ioRadar701.md`。

## 2026-10-31：五輪 SHADOW 實機結果最終離線核對

- 在不開 UART／I²C／GPIO、不中斷服務、不調 NTP／校正／門檻的前提下，核對指定五輪
  live 各七檔。raw bytes／SHA-256、JSON／JSONL、metadata／summary、parser、行數、
  recording completeness 與 cleanup 全部一致；五輪皆完整保存、errors 空。第 4 輪
  尾端 4 bytes 且最後 cycle 597 未封閉，已明確區分「保存完成」與「週期封閉」。
- 使用目前同核心 replay 逐輪輸出到 `shadow-results/final-offline-20261031/`。每輪
  decoded／BNO／shadow／events 四個 JSONL 與 live 逐 byte 相同，summary/parser/counts
  相同；原 capture 全保留。
- v2 每 receipt batch 至多一次 applied；五輪各 1 coalesced、0 time discontinuity，
  全部 GPIO false。matched BNO 因果違規 0，最大 age 0.10217–0.10482 s。
- 首輪 `1194 - 1192 - 1` 的剩餘列是 cycle 939 的 no-target/not-applicable。它在約第
  47 秒正確清 target-bound state，cycle 940–952 重暖、953 恢復；BNO window 仍有 10
  筆，不是 BNO 掉線或時鐘問題。
- 每輪第一筆 BNO 為啟動無效值，其後 broad quality 通過；但 system／accelerometer／
  magnetometer calibration 全程 0，不能宣稱已校正。第三輪只有雷達移動、Pi/BNO 固定，
  不能作共同運動驗收；操作員已確認第五輪實際為全部設備固定、人員正面移動。該場景
  已結案，不需重測；因無 stage event／target identity 真值，仍不外推方向或道路驗收。
- 本輪實跑 hardware-free regression 318 passed in 2.18s；未跑 full suite，歷史
  63／455／1381 不算本輪。沒有 stage/commit/push 或清理 Git。
- 完整五輪表、raw SHA、分類/reset、replay 路徑、命令、缺件與限制：
  `docs/ioRadar701_BNO055_Shadow_實機測試結果_2026-10-31.md`；續作斷點：
  `docs/進度斷點_2026-10-31_ioRadar701.md`。

## 2026-10-27：Shadow 同時間戳週期核對、最小修正與真實保存資料 replay

- 已唯讀核對 `shadow-results/live-20261027T213626.007934+0800/`。原始 22464
  bytes、594 status、1278 target、1872 decoded、272 BNO、594 shadow，parser
  checksum/sync/trailing 皆 0；`recording_complete=true`、errors/cleanup_errors 空。
  原始 cycle 1／2 共用 `363.059385986`，第二列確實被舊程式 reset 成 1 次
  `radar_time_discontinuity`；原 capture、summary 與 event 均未改寫。
- 根因已由程式確認：live 一次 `serial.read(4096)` 只取得一個 host-monotonic，再將
  整個 chunk 送入一次 feed；parser/state 可在同 feed 正常發出多個不同 cycle，沒有
  重複 emission 或 replay 重送。舊 shadow core 逐 cycle 送入嚴格遞增 estimator，且
  把相等與真正倒退合成同一 non-increasing reset。不是 NTP 或 radar internal clock
  的證據。
- 最小修正限定在 shadow input coordination/capture/replay：一個 receipt batch 最多
  一次 fusion update，使用最後已封閉週期；同批較早週期仍保存為
  `fusion_update_coalesced`。selector/reset 仍依序執行，最後 N=0／incomplete 不回退
  舊 target，target switch 不跨目標留 trend；跨批相等、真正 backwards、large gap
  仍各自失效。沒有 epsilon／假 50 ms／新時鐘補值，BNO causal quality/dedup 不變。
- 保存 schema 升為 additive v2：decoded/shadow 新增 `receipt_batch_sequence`，shadow
  新增 `fusion_update`、coalesced cycle 與 `time_relation`；summary 新增 batch、applied、
  coalesced、discontinuity 計數。新 replay 可讀 v1；若 v1 沒 batch ID，只能把連續完全
  相同 receipt timestamp 推定同批，metadata 明記不是原 callback boundary 的證明。
- 真實保存資料已純離線 replay 到
  `shadow-results/replay-20261027T221839.269770+0800/`：1872 decoded、272 BNO、
  594 shadow、625 推定 batches、592 fusion applied、1 coalesced、0 time
  discontinuity，parser/保存完整。修正前後 matched 589、invalid motion 3、fusion
  valid 578/ready 456/baseline 205、分類 448 stable_or_receding＋144 uncertain、
  warning 593、no-target 1 全不變；只把原 1 time discontinuity 改為 1 audit-visible
  coalesced。首筆 invalid BNO 仍拒絕，沒有降低品質閘門。
- 本輪實跑 targeted 318 passed（ioRadar、radar sync、fusion、warning、shadow CLI）；
  三層 help、兩檔 py_compile、真實 replay、`git diff --check` 也通過。沒有重跑 full
  suite，舊 63／455／1381 只是歷史。沒有開 UART/I2C/GPIO/藍牙/systemd，沒有
  stage/commit/push。修正後新 batch ID 尚待下一次明確授權的 live 驗證。
- 完整根因、schema、前後統計、命令與限制：
  `docs/ioRadar701_BNO055_Shadow_實作與驗證_2026-10-27.md`；續作斷點：
  `docs/進度斷點_2026-10-27_ioRadar701.md`。`docs/CODEX_PROJECT_TRANSFER_TASK.md`
  與 `docs/evidence/ioradar701/` 四份 Windows JSON 現地仍缺少，未等待它們。

## 2026-10-27：ioRadar701＋BNO055 Shadow 實作與純軟體驗證（修正前歷史）

- 已新增 `python -m rear_warning.cli.ioradar701_shadow replay|live`；既有
  `ioradar701 offline|live` 與 TFMini `rear-warning-live` 未取代。新入口固定
  `SHADOW／不控制實體警示`，程式沒有 GPIO／LED adapter 或啟用參數。
- `RadarStateTracker` 現會一次性發出 complete、no-target 與被下一 status 封閉的
  incomplete cycle。收集 targets 期間不 reset；封閉不完整、N=0、target 切換、
  stale/error 或 non-increasing cycle timestamp 才清除 target-bound trend、
  classifier baseline/candidate 與 warning stabilizer。健康 BNO window保留，且同一
  canonical motion timestamp 不重複更新自身動作估計。
- selector 第一版規則固定為「目前 ID 仍在就保持；否則按 `hypot(X,Y)` 最小、
  再按 ID 平手排序」。保存全部候選、選擇／保持／切換理由；這只是開發用策略，
  不是威脅排序、車道過濾或永久物體追蹤。X/Y/Vx/energy/raw 全保存，Vx 不進入
  closing rate、TTC 或絕對車速。
- 雷達使用完整週期完成時的 host-monotonic receipt timestamp；BNO canonical time
  沿用既有同步層語意，為成功 read 後的 host monotonic，model 內 read-start time
  另存。重播使用 decoded 保存的 host elapsed；同一 read 內重複 cycle timestamp
  明列 `radar_time_discontinuity` 並 reset，不微加時間偽造順序。
- 現地沒有同時段 BNO055 保存檔。六輪結果標成「REPLAY／真實雷達＋無 BNO055
  記錄」，motion 不補成靜止，融合為 not-ready；距離 WarningPolicy 候選獨立計算。
  六輪 replay 全部 `recording_complete=true`，frames/shadow records 為
  1804/597、2728/596、1381/589、7030/2386、489/152、257/107；T4 parser 仍為
  1 sync discard、0 checksum failure、0 trailing。
- replay 產物位於 `shadow-results/replay-20261027T171228.597171+0800` 至
  `replay-20261027T171230.678050+0800` 六個目錄，已加入 `.gitignore`；原 capture
  沒有修改。每輪保存 decoded/BNO(empty)/shadow/events、metadata、summary。
- 本輪硬體無關驗證：新增核心範圍 63 passed；跨 ioRadar/sync/fusion/warning/
  舊 LIVE 455 passed；全專案 1381 passed，皆 0 failed/0 skipped。另執行三層
  CLI help、六輪 replay 及 `git diff --check`。
- **當時未執行**：真實 shadow live UART/I²C、GPIO/LED、藍牙、systemd、道路、距離
  精度、T2/T5、BNO axis/profile 部署確認。live 命令存在但須操作員檢查 IO16→
  Pi Pin 10/GPIO15、共地、230400/8N1、I2C bus 1/address 0x29 及 owner 後手動 Enter。
- 完整命令、schema、限制與 rollback：
  `docs/ioRadar701_BNO055_Shadow_實作與驗證_2026-10-27.md`；今日可續作斷點：
  `docs/進度斷點_2026-10-27_ioRadar701.md`。

## 2026-10-27：ioRadar701 實測結案與 BNO055／融合／LED 整合盤點

- 本輪只做保存檔唯讀核對、共用 parser 離線重播、原始碼盤點與文件更新；沒有開啟
  UART／I²C、掃描匯流排、操作 GPIO／LED／systemd、重跑實機採集或修改 runtime code。
- 已核對六個 2026-10-27 capture 目錄，每輪 `raw.bin`、`decoded.jsonl`、
  `events.jsonl`、`metadata.json`、`summary.json` 都存在且可讀。共用 parser 重算的
  bytes、status/target、checksum、sync discard、trailing 與週期品質全部吻合 summary；
  decoded 幀也可重組 raw（T4 除外的唯一差異正是前置一 byte）。raw 雜湊、幀率分母、
  zero-target 分布與完整數字見結果文件。
- T4 唯一 sync discard 已定位：raw 零起算 offset 0 為 `0xff`；offset 1 與 13 是
  第一、第二個合法 12-byte 幀起點。parser 丟棄前置 byte 後立即恢復，全檔 checksum
  failure=0、trailing=0。只能判定為起始前置非幀 byte，來源未知；沒有證據宣稱雷達
  故障或精確少一幀。
- 六輪已結束週期皆為完整，duplicate/extra/orphan 全為 0。zero-target status：
  T1=31、T3=1、T4=31、T6 120s=17、restart1=1、restart2=0。最大有效幀主機接收
  間隔皆約 50.66–51.03 ms；1 秒顯示門檻下沒有保存到 stale/recovery 證據，但未保存
  每次 UI snapshot，不能宣稱端到端延遲或個別 target update rate。
- T6 120 秒為 `completed`，兩次重啟是預期 Ctrl+C 的 `interrupted`。三輪 metadata／
  summary 都明記 serial close、capture files close、full capture saved、summary saved
  為 true 且 storage errors 空；操作員另確認回 shell 並能開下一輪。未驗證第四輪、
  重開機、service 恢復或長期運作。
- T3 只採信操作員觀察「即時數值會隨移動更新」；events 沒有動作階段標記，不判定
  距離方向或 Vx 正負。T4 無法辨認本人 target，消失／返回仍待確認。T2 距離精度與
  T5 雙人場景未執行，多 ID 不當作雙人辨識證據。
- 歷史 T0 本輪以共用 parser 重算，仍為 3168 bytes、SHA-256
  `68ce90c6b803add133da7eae19c7a258ed6f97555ccb0ff46db55d7709456d04`、100 status、
  164 target、0 checksum/sync/trailing、100 輪完整。先前 40／57／1358 passed 沒有
  在本輪重跑，只是歷史結果；本輪沒有改程式，依任務只做文件／資料驗證。
- 建議唯一整合路徑：保留 standalone `ioradar701 offline|live` 與舊 TFMini LIVE，
  另加單程序／單 UART owner 的 ioRadar＋BNO055 入口；先 shadow 記錄 target selector、
  host-monotonic causal sync、既有 trend/own-motion/classifier、距離 warning candidate
  與可檢視理由，待 LED decision table、座標／target switching 與實體接線確認後才
  以明確 flag 啟用 GPIO。
- 多目標不得取第一筆：只從完整新鮮週期選 target，短期保持 eligible current ID，
  新候選須通過尚待批准的 corridor／switch margin／confirmation；N=0、未完整、stale、
  error、ID 消失或切換必須中斷並清除 target-bound trend/classifier。使用
  `hypot(X,Y)`；X/Y/Vx 分開保存，Vx 不直接送入 closing rate、TTC 或車速估計。
- 零目標、未完整、過期、裝置錯誤、BNO 未 ready、sync unmatched、fusion warmup
  必須保留不同 availability，不能補 0 或綠燈。現行沒有 relative-class→LED 規則；
  `NO_TARGETS` 的實體 LED 投影、實際 LED pin/active level、雷達／BNO 車體軸向、
  selector 參數與 BNO calibration profile 是啟用硬體前的具體待決策。
- `docs/CODEX_PROJECT_TRANSFER_TASK.md`、兩份 2026-10-18 ioRadar 文件與 512-byte
  evidence 仍未在現地找到；沒有假稱 Windows/Pi 已同步。專案 `systemd/` 目前也沒有
  可核對的 `tfmini.service` unit，只有操作員回報的外部占用歷史；未來入口只做 owner
  preflight 並拒絕 busy，不自動 stop/start service。
- 實測結案：`docs/ioRadar701_解析與實機測試結果_2026-10-27.md`。介面表、資料流、
  target 選擇／reset、最小檔案變更、測試矩陣、生命週期與 rollback：
  `docs/ioRadar701_BNO055_LED_整合盤點與方案_2026-10-27.md`。

## 2026-10-27：ioRadar701 解析、離線全檔與實機工具準備（實測前階段）

- 新增硬體無關 `rear_warning.sensors.ioradar701`：12-byte 串流 parser 可處理任意
  分段、半幀、多幀、垃圾、壞 checksum 後重新同步；狀態週期依宣告 N 與不同 ID
  組合，下一輪／N=0 清舊值，bytes／狀態／目標 freshness 分開。
- 新增 `python -m rear_warning.cli.ioradar701 offline|live`。offline 與 live 共用同一
  parser；live 單一程序只讀 230400/8N1 UART，預設 1 秒顯示過期與 5 Hz 中文面板，
  支援 `--plain`、`:event`、`:stop`，保存 raw／decoded／events／metadata／summary。
- 歷史成功檔 `capture-20261012T132538.259621+0800/raw.bin` 實算 3168 bytes、SHA-256
  `68ce90c6b803add133da7eae19c7a258ed6f97555ccb0ff46db55d7709456d04`；100 狀態幀、
  164 目標幀、0 checksum failure、0 sync discard、0 trailing byte。目標 ID 0／1／2；
  100 輪均按宣告的 1 或 2 個目標完整組合。原檔未修改。
- raw.bin 無逐幀時間；19.9720 status fps／32.7541 target fps 只以 metadata 的
  5.007007238 秒作整輪分母，不能宣稱延遲或最長幀間隔。Vx 全為 0 也不能當作方向
  或速度精度驗證；有符號欄位仍依任務書 offset 公式，原廠歧義未覆核。
- 任務書提到的 2026-10-18 原廠核對／前階段任務／512-byte evidence 與
  `docs/CODEX_PROJECT_TRANSFER_TASK.md` 均不在現地；已明列證據限制。
- ioRadar701 新增測試 40 passed；連同既有 raw receiver 為 57 passed，全專案為
  1358 passed。均為真實片段、完整保存檔解析或
  fake／合成軟體測試；沒有冒充實機場景。該階段未開 UART、未操作 systemd／GPIO／
  I2C／藍牙。T0 通過；T1–T6 在當時仍未執行，後續結案狀態以上一節為準。
- 當時的完整命令、T0 數值與 T1–T6 操作表後來已由同一路徑的結案內容取代：
  `docs/ioRadar701_解析與實機測試結果_2026-10-27.md`。這段保留為實測前的歷史，
  不代表整套警示系統已完成。

## 2026-10-16：ioRadar701 UART 原始資料接收軟體準備

- 新增獨立 `tools/ioradar701_raw_receive.py`，預設 `/dev/ttyAMA0`、256000 baud、
  候選 8N1、無流控與 5 秒。Enter 前只顯示設定；EOF／Ctrl+C 取消且不開 UART。
- 工具只接收 opaque bytes，不送命令、不清 input buffer、不套 TFMini parser。
  使用 monotonic deadline；正常、零資料、中止與錯誤路徑都會嘗試關閉本次資源。
- 每輪在 `captures/ioradar701/capture-時間戳/` 建立不覆寫的 `raw.bin` 與
  `metadata.json`；終端只顯示最前 512 bytes 的 hex。擷取資料已加入 `.gitignore`。
- 操作與安全邊界見 `docs/ioRadar701_UART原始資料接收.md`。目前缺少 P1 Pin 10／
  綠線確為雷達 TX，以及訊號高電位可直接接 Pi 3.3V UART 的可信證據，故本輪沒有
  接線、開啟真 UART 或執行 5 秒實體採集。目前階段僅為「軟體準備完成」。
- 新增硬體無關測試，涵蓋 help／非法參數／等待取消不開埠、完整 bytes 保存、
  512-byte sample 上限、零資料、Ctrl+C、讀取／開埠／寫檔／關閉錯誤。
- 實際執行目標測試為 17 passed；`py_compile`、`--help`、非法 duration 與 EOF
  取消檢查皆符合預期。所有檢查均未開啟真 UART。
- 本輪保留原有未提交 LIVE、demo、fusion 與文件成果；沒有 stage、commit、push，
  沒有操作 GPIO、I2C、藍牙、systemd 或改動正式警示／parser。

## 2026-10-05：僅調整 LIVE 固定面板（最新）

本輪取代下節初版「逐行輸出優先」的顯示偏好；不新增感測實驗或硬體操作。

- 原命令在 ANSI TTY 預設使用約 5 Hz 中文固定面板，Enter 前提示與硬體確認
  門檻不變。`--plain`／`TERM=dumb` 使用純文字；輸出類別也支援非 TTY 純文字，
  但沒有放寬 CLI 本來拒絕非互動啟動的條件。
- 主要修改 `src/rear_warning/live/terminal.py`：公開 snapshot、CJK／ANSI 寬度、
  換行、雙欄／單欄、最多三筆歷史事件、原位覆寫與游標／替代畫面恢復。
  區分待確認、最近確認分類、候選與原因；暖機不當成讀取故障，過期不留舊值。
  不足以容納必要內容時保留原因並切回純文字，本輪不再自動切回面板。
- CLI 僅接上顯示模式、初始化畫面、短行摘要與終端機還原後的備援錯誤回報。
  沒有改 coordinator、採樣、時間配對、fusion、閾值、硬體設定或服務。
  沿用非阻塞有限緩衝；部分 frame 先完成再還原，未顯示事件／畫面計入丟棄。
- 說明：`docs/終端機即時偵測.md`、README；可查驗的版面文字在
  `docs/終端機面板預覽.txt`。由 fake devices 經真正 coordinator／fusion，再經
  Linux pseudo-TTY 擷取，事件為測試注入，始終標示 SIM；不是硬體紀錄或圖片截圖。
- 新增 `tests/cli/test_live_terminal.py` 21 個測試，既有 LIVE 測試僅擴充 fake
  terminal 介面。涵蓋候選／暖機／不確定／過期、非 TTY／dumb／plain、CJK、
  長原因、事件順序與丟棄、慢輸出、部分 ANSI frame、中止、初始化／顯示失敗、
  broken pipe 與終端機還原。fake CLI 正常／中止／失敗均確認資源 close 與摘要；
  真正連線中斷時還原只能盡力嘗試，不能保證已斷線的對端收到 escape 或摘要。
- 實際 PTY：80×24、120×30、48×30；另驗證長原因完整換行、80×24 → 100×30
  清除舊行、25×12 回退純文字。未驗證實體 SSH／VS Code 字型呈現。

本輪實際命令與結果（均為 0 failed／0 skipped）：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider tests/cli tests/synchronization tests/fusion
# 455 passed
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider
# 1301 passed
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q -s tests/cli/test_live_terminal.py -k real_pty_layouts
# 3 passed（其餘 deselected）
```

Git 分支／HEAD 不變：`codex/sensor-fusion-motion-classification`／
`3d6d71b5f1eb9d980d6a976d6ddf0ab4da9c0685`。保留所有既有 WIP；既有
`.gitignore`、CHANGELOG、pyproject、coordinator、fusion test 及 demo 的修改
不是本輪新增。本輪新增未追蹤檔為 `tests/cli/test_live_terminal.py` 與
`docs/終端機面板預覽.txt`；其餘 LIVE／交接檔本來即為 untracked。
沒有 stage、commit、push，沒有新增或改動 HTML。

## 2026-10-05：手動啟動終端機 LIVE 偵測

目前主要入口改為 `python -m rear_warning.cli.live_detection`；完整命令及欄位說明
見 [終端機即時偵測](docs/終端機即時偵測.md)。前次 HTML／JSON／demo 與測試保留。

- `--confirm-hardware` 與互動 Enter 之前不建立硬體 adapter。按 Enter 後依既有
  device layer 初始化 UART／BNO055，經真正 coordinator 與 fusion adapter 判斷。
- coordinator 新增可選 `on_tick`、`should_stop`、最近有效 motion timestamp
  與 startup valid motion count。既有 bounded callers 保持原行為；LIVE 可持續至
  Ctrl+C，仍保留停滯時鐘及有限 readiness guard。
- 終端機預設 5 Hz，逐筆推理頻率不受刷新率影響。以一筆最新狀態、16 筆歷史
  事件及至多一筆部分輸出實作有界非阻塞傳送，回報丟棄數。
- 超過 0.25 秒未取得距離時，畫面顯示過期／UNCERTAIN。恢復後由現行 adapter
  依時間間隔清除推理狀態，沒有另寫分類器或降低基準門檻。
- 唯讀 fuser preflight 與 UART exclusive/TIOCEXCL；裝置忙碌直接回報，不操作
  systemd。按 Ctrl+C、SIGHUP／SIGTERM、讀取錯誤或輸出失敗時 cleanup，保留
  主因與 cleanup errors。五秒 watchdog 為 userspace 保護，不保證中止 kernel hang。
- 本輪開始時工作樹已有前次未提交成果；保留原分支
  `codex/sensor-fusion-motion-classification`／HEAD `3d6d71b5f1eb9d980d6a976d6ddf0ab4da9c0685`。
  本輪重新執行基準 full suite：1237 passed。
- 完成後 targeted 329 passed、full 1280 passed，0 failed／0 skipped。
  新增 43 個 LIVE 測試（互動 gate、真正 coordinator/fusion 串接、刷新獨立、
  斷流／恢復、bounded output、signals、部分初始化、複合 cleanup、裝置占用）。
  fake 正常與中止路徑已確認資源 close；注入 cleanup 失敗會回報 false／非零。
  實際 PTY 只驗證 Enter 前等待與 Ctrl+C，未開啟感測器。
- 本輪環境缺少 `/dev/ttyAMA0`／`/dev/i2c-1`，未開始實機讀取。命令參數取自
  現行設定及既有證據：115200 baud、bus 1/address 0x29。

下一步由操作員在樹莓派照手冊完成 Enter 前等待、固定平台 warmup、移動目標／
平台、Ctrl+C cleanup、第二輪重啟，填入真實觀察。尚未驗證實機啟停、通訊時限、
距離精度或道路行為；沒有加入融合到 LED、GPIO、藍牙、校正檔或服務操作。

## 2026-10-04：競賽錄影離線展示

### 已完成

- 新增硬體無關 `rear_warning.demo`，以決定性合成 TFMini Plus／BNO055 model
  經 `SensorSynchronizer` 與 `RelativeMotionFusionAdapter` 產生 A–D 四個情境。
- 距離警示欄另以全新的 `WarningPolicy`／`WarningStateStabilizer` 計算，沒有建立
  fusion-to-LED 映射，也沒有載入 GPIO adapter。
- 新增單檔、無外部資源的繁中 HTML player，以及完整 JSON trace。trace 記錄
  實際預設參數、sample timestamps、輸入／輸出摘要、Git HEAD 與 dirty 狀態。
- 新增展示 acceptance tests；另保留並完成使用者已開始的 invalid-distance
  regression 強化：先建立並 assert stationary baseline，再驗證 invalid distance
  不更新既有 trend、清除 baseline 且 fail-safe。
- 新增一般組員可執行的生成、播放、實體 LED 拍攝與三分鐘分鏡文件。
- 使用 Chromium 151 以 1600×900 載入單檔 HTML 並檢查截圖；主要資料、曲線、
  分類、距離警示與控制列均在單一 16:9 畫面內。瀏覽器內建自我檢查實際觸發
  播放、暫停、重播、進度、情境切換及 4×，結果為 `passed`。

### 設計決策

- 使用 20 Hz distance／10 Hz motion 減少離線檔案體積，但不改 production
  estimator／classifier 門檻。每個唯一 motion sample 仍由 adapter 去重。
- HTML 不計算 classification；它只播放 Python 公開輸出。播放速度不改 timestamp。
- invalid distance 使用既有 `distance_cm=0` rejection contract；stale 使用既有
  synchronizer age contract。畫面明確稱為軟體注入，不稱為拔線實測。
- 每次情境運算建立全新有狀態元件；回放、拖曳與重播只移動 frame index。

### 尚未驗證／不在本輪範圍

- 本輪沒有操作 UART、I²C、GPIO、藍牙、systemd 或校正設定檔。
- 實體 LED 邊界與現行 hysteresis 尚待操作員依文件拍攝與記錄。
- 尚未完成 BNO055＋TFMini 融合接入 production LED、道路驗證或競賽影片剪輯。
- 實際錄影電腦的字型、全螢幕縮放與錄影軟體擷取效果仍需在拍攝前人工確認；
  headless 截圖不代表影片已錄製。

### 下一步

1. 在錄影電腦離線開啟 `demo/rear-warning-demo.html`，人工檢查中文字型、16:9
   排版、播放／暫停／重播／切換／倍速及錄影軟體擷取效果。
2. 操作員確認硬體與 UART owner 後，另行授權一次 bounded 實體距離＋LED 拍攝。
3. 將本次拍攝日期、HEAD／dirty、參數與實際觀察填回拍攝紀錄；未做項目保留
   「待拍攝／待驗證」。
