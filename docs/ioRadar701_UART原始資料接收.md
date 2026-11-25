# ioRadar701 UART 原始資料接收

> 2026-10-27 更新：本文件保留 2026-10-16 原始接收工具的歷史安全邊界。
> 後續已確認的 230400 baud 接線、解析器、完整檔結果與新 live 操作請以
> [ioRadar701 解析與實機測試結果](ioRadar701_解析與實機測試結果_2026-10-27.md)
> 為準；不要同時執行本工具與新 live 工具搶讀同一 UART。

## 目前階段

目前是「軟體準備完成」，不是「已收到 bytes」、「已驗證封包」或「已解析量測」。
本工具不使用 TFMini parser，也不把任意 UART bytes 解釋成距離或速度。

商品頁提供的第一輪候選設定是 `/dev/ttyAMA0`、256000 baud、8N1、無流控。
其中 8N1 仍是候選格式。藍板 P1 Pin 10／綠線是否確為雷達 TX，以及該訊號是否
可直接接 Raspberry Pi 的 3.3V UART，目前都缺少可信證據。因此訊號線應保持未接，
不可把供電為 5V 當成 UART 高電位相容證明。

## 實機執行前必要條件

操作員必須先在斷電狀態下確認：

1. 原廠腳位資料或可靠量測已證明雷達 TX 的實際線路。
2. 雷達 TX 高電位不超過 Raspberry Pi UART 可接受的 3.3V；必要時使用合適的
   電平轉換，Python、VS Code、DTR/RTS 設定都不能降低訊號電壓。
3. 雷達與 Pi 已共地，且雷達供電接法與極性已確認。
4. TFMini 的 TX 已從 GPIO15／Pi 實體 Pin 10 隔離，沒有第二個程序或感測器占用
   `/dev/ttyAMA0`。`tfmini.service` inactive 不代表實體訊號已拔除。
5. 第一輪只連雷達 TX 到 Pi 實體 Pin 10（GPIO15/RXD0）；Pi 實體 Pin 8/TXD0
   不接雷達 RX。

條件未全部成立時，只能執行下面的 `--help` 與 EOF 取消檢查，不能按 Enter 開埠。

## 命令

顯示說明，不開 UART：

```bash
cd /home/topics/Projects/Smart-Rear-Collision-Warning-System
.venv/bin/python tools/ioradar701_raw_receive.py --help
```

第一輪 5 秒只讀擷取（僅在上述條件已全部確認後執行）：

```bash
cd /home/topics/Projects/Smart-Rear-Collision-Warning-System
.venv/bin/python tools/ioradar701_raw_receive.py \
  --port /dev/ttyAMA0 \
  --baudrate 256000 \
  --duration 5
```

程式會先列出設定並等待 Enter。EOF 或 Ctrl+C 會在開埠前取消。按 Enter 後才會：

- 以 256000 baud、8N1、無軟／硬體流控開啟 `/dev/ttyAMA0`。
- 在 POSIX 平台要求 pySerial `exclusive` 協作式獨占。
- 將 DTR 與 RTS 設為 false；這不是電平轉換。
- 只呼叫 UART `read()`，不呼叫 `write()`、不送初始化或重設命令，也不清空輸入。
- 使用 monotonic deadline 限制時間，最後關閉本次 UART 資源。

`--duration` 必須大於 0 且不得超過本任務授權的 5 秒上限。

若作業系統拒絕 256000，程式會直接回報錯誤，不會改用 115200 或掃描其他速率。
遇裝置忙碌或權限錯誤時，不會 kill 程序、啟用 sudo 或操作 systemd。

## 輸出與判讀

每輪會在 `captures/ioradar701/capture-時間戳/` 建立不覆寫的目錄：

- `raw.bin`：本輪實際收到的完整原始 bytes。
- `metadata.json`：埠、要求速率／8N1、起訖時間、實際秒數、bytes 數、退出原因、
  關閉結果與是否完整保存。

終端機只顯示最前 512 bytes 的十六進位 sample，不輸出原始控制字元。若
`total_bytes > 0`，只能報告「已收到原始 bytes」；在取得可信的封包邊界、長度、
校驗及單位資料前，不能稱為有效雷達封包或量測值。零資料也不能單獨證明雷達損壞。

## 無硬體驗證

```bash
cd /home/topics/Projects/Smart-Rear-Collision-Warning-System
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  tests/tools/test_ioradar701_raw_receive.py
```

測試使用 fake UART，不會開啟 `/dev/ttyAMA0`，也不操作 GPIO、I2C、藍牙或 systemd。
