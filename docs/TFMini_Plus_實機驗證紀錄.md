# TFMini Plus 實機驗證紀錄

## 一、驗證範圍

本文件記錄 TFMini Plus 第一階段實機驗證結果。驗證範圍包含標準 UART 資料輸出、UART 裝置層、純封包解析器及命令列診斷工具，不包含資料濾波、IMU 融合、碰撞警示策略、LED 或藍牙輸出。

完整測試原始日誌保留於本機 `logs/`，不納入 Git；本文件只保存驗證所需的統計摘要與操作條件。

## 二、測試平台

- 硬體平台：Raspberry Pi 5 Model B Rev 1.1
- 系統架構：AArch64
- 作業系統：Debian GNU/Linux 13.2（trixie）
- Linux Kernel：`6.12.47+rpt-rpi-2712`
- pyserial：3.5

## 三、實際 UART 設定

- UART 裝置：`/dev/ttyAMA0`
- Baudrate：115200
- Timeout：0.1 秒
- 感測器輸出：TFMini Plus 標準 9-byte、cm 單位資料封包

## 四、軟體資料鏈

```text
TFMini Plus
    ↓
/dev/ttyAMA0
    ↓
TFMiniPlusSerialDevice
    ↓
TFMiniPlusParser
    ↓
tfmini-plus-diagnostic
```

UART 裝置層只負責序列埠生命週期與位元組讀取；所有讀取資料均交由 `TFMiniPlusParser` 處理，命令列工具不直接操作 pyserial，也不重新實作封包解析。

## 五、舊版 systemd 服務

- 舊版服務檔為 `/etc/systemd/system/tfmini.service`。
- 該服務會開啟並占用 `/dev/ttyAMA0`。
- 執行新版診斷工具前必須先停止 `tfmini.service`。
- 不可讓舊版服務與新版診斷工具同時開啟同一個 UART 裝置。
- 測試完成後需重新啟動舊版服務，恢復原有功能。

停止或啟動服務會中斷或恢復既有警示程式，並改變 UART 的占用狀態。下列流程只能由已了解影響並取得授權的操作人員執行，不得由自動化工具自行執行 `sudo` 或變更服務狀態。

## 六、建議操作流程

### 1. 停止舊版服務並確認 UART 已釋放

```bash
sudo systemctl stop tfmini.service
systemctl is-active tfmini.service
lsof /dev/ttyAMA0
```

執行診斷工具前，`tfmini.service` 應為非執行狀態，且 `lsof` 不應顯示其他程序持有 `/dev/ttyAMA0`。

### 2. 執行 30 分鐘診斷

```bash
.venv/bin/tfmini-plus-diagnostic \
  --port /dev/ttyAMA0 \
  --baudrate 115200 \
  --timeout 0.1 \
  --duration 1800
```

### 3. 測試後恢復舊版服務

```bash
sudo systemctl start tfmini.service
```

重新啟動後應另行確認服務狀態；不得讓服務與診斷工具同時持有 UART。

## 七、短時間測試結果

- 有效量測數：`500`
- 空讀取／timeout 次數：`0`
- 執行時間：`5.116` 秒
- 平均有效資料率：`97.737 Hz`

原始統計輸出：

```text
valid_measurements=500
empty_reads=0
elapsed_seconds=5.116
average_valid_rate_hz=97.737
```

短時間測試確認 UART 裝置、Parser 與命令列輸出能形成完整資料鏈。

## 八、中途測試結果

- 有效量測數：`87181`
- 空讀取／timeout 次數：`0`
- 執行時間：`888.687` 秒
- 平均有效資料率：`98.101 Hz`
- 此次測試未達完整 30 分鐘，僅作為長時間執行過程的中途觀察紀錄。

原始統計輸出：

```text
valid_measurements=87181
empty_reads=0
elapsed_seconds=888.687
average_valid_rate_hz=98.101
```

## 九、完整 30 分鐘測試結果

- 有效量測數：`175965`
- 空讀取／timeout 次數：`0`
- 執行時間：`1800.048` 秒
- 平均有效資料率：`97.756 Hz`
- 最近一次有效量測時間：`2026-08-01T18:32:04.403+08:00`

原始統計輸出：

```text
valid_measurements=175965
empty_reads=0
elapsed_seconds=1800.048
average_valid_rate_hz=97.756
last_valid_measurement_at=2026-08-01T18:32:04.403+08:00
```

測試完成後以以下錯誤關鍵字搜尋日誌：

```bash
grep -Ei "traceback|exception|error|failed" <測試日誌>
```

搜尋沒有任何輸出，表示日誌中未找到 `traceback`、`exception`、`error` 或 `failed` 關鍵字。

## 十、驗證結論

- `TFMiniPlusParser` 通過 TFMini Plus 實體資料驗證。
- `TFMiniPlusSerialDevice` 通過 `/dev/ttyAMA0` UART 通訊驗證。
- `tfmini-plus-diagnostic` 通過短時間、中途及完整 30 分鐘執行驗證。
- Parser、UART 裝置層、CLI 診斷工具及實體硬體資料鏈均通過第一階段驗證。
- 完整 30 分鐘內共取得 `175965` 筆有效量測，空讀取次數為零。
- 完整測試的平均有效資料率為 `97.756 Hz`，約為 `97.8 Hz`。
- 測試日誌未發現指定的錯誤關鍵字。
- 軟體回歸測試結果為 `62 passed`，全部自動測試均通過。

本階段結果僅證明 TFMini Plus 測距資料鏈能穩定運作；後續濾波、IMU 融合與碰撞警示策略仍需分階段設計與驗證。
