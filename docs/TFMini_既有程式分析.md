# TFMini Plus 既有程式分析

## 一、分析範圍與前提

本文件以靜態閱讀方式分析下列既有實驗程式，未執行任何程式、未開啟 UART，也未連接或操作硬體：

- `experiments/legacy_tfmini/20260713_test.py`
- `experiments/legacy_tfmini/live_lidar.py`
- `experiments/legacy_tfmini/live_lidar2.py`
- `experiments/legacy_tfmini/pandasTF.py`
- `experiments/legacy_tfmini/tfmini_test.py`

`docs/TFMini_UART_開發計畫.md` 記錄的是專案初始化階段的狀態，其中提到 `AGENTS.md` 與 `experiments/` 尚無內容；目前 Repository 已經有操作規範及上述五支實驗程式，因此該段內容應視為當時的狀態快照。

### 1.1 資訊來源與可信度

本文件嚴格區分下列資訊：

1. **使用者已確認的硬體資訊**：本專案實際使用的是 **Benewake TFMini Plus**，不是一般 TFMini，也不是 TFMini-S。這是目前專案的硬體事實。
2. **從既有程式觀察到的行為與參數**：包含 `/dev/ttyAMA0`、115200 baud、9-byte 封包、`0x59 0x59` 幀頭、欄位公式及既有篩選條件。這些內容只代表舊程式如何實作，不等同於已核對官方規格。
3. **尚待官方資料表確認的規格**：封包結構、checksum 規則、欄位定義、單位、有效範圍、訊號品質門檻、預設輸出頻率及 UART 設定等，仍須以 Benewake 官方 TFMini Plus 資料表或通訊協定文件逐項確認。
4. **不得作為型號證據的內容**：舊程式檔名、圖表標題及註解不能用來推論其他硬體版本，也不能取代使用者提供的硬體確認資訊。

所有實驗程式都把 UART 讀取、封包解析、資料篩選及輸出或繪圖放在同一支程式中，尚未符合目前 `AGENTS.md` 要求的模組邊界。以下分析目的為辨識可沿用的候選邏輯及正式實作前必須修正的風險，不建議直接把任一實驗程式搬入正式套件。

## 二、整體比較

| 程式 | 主要用途 | UART 裝置路徑 | 鮑率 | 既有程式的封包假設 | Checksum | 實際輸出 |
|---|---|---:|---:|---|---|---|
| `20260713_test.py` | 即時讀取、繪製最近 50 筆距離，並持續附加詳細 CSV | `/dev/ttyAMA0` | 115200 | 9 bytes，開頭為 `0x59 0x59` | 未驗證 | 距離、訊號強度、溫度與時間戳寫入 CSV；圖表顯示距離 |
| `live_lidar.py` | 使用 Matplotlib 動畫顯示最近 100 筆距離，並建立詳細 CSV | `/dev/ttyAMA0` | 115200 | 9 bytes，開頭為 `0x59 0x59` | 未驗證 | 距離、訊號強度、溫度與時間戳寫入 CSV；圖表顯示距離 |
| `live_lidar2.py` | 較精簡的即時距離圖表，每五筆記錄一次 CSV | `/dev/ttyAMA0` | 115200 | 9 bytes，開頭為 `0x59 0x59` | 未驗證 | 只記錄時間與距離 |
| `pandasTF.py` | 用 Pandas 維護最近 50 筆距離，使用動畫繪圖並附加 CSV | `/dev/ttyAMA0` | 115200 | 9 bytes，開頭為 `0x59 0x59` | 未驗證 | 只記錄時間索引與距離；訊號強度和溫度雖有解析但未使用 |
| `tfmini_test.py` | 最小化命令列監看工具，連續印出量測值 | `/dev/ttyAMA0` | 115200 | 9 bytes，開頭為 `0x59 0x59` | 未驗證 | 在終端顯示距離、訊號強度與溫度 |

`tfmini_test.py` 的註解建議失敗時可嘗試 `/dev/serial0`，但實際程式常數仍是 `/dev/ttyAMA0`，因此五支程式真正使用的裝置路徑完全相同。

## 三、從既有程式觀察到的封包假設與解析方式

本章只描述舊程式的實際行為，不表示相關參數已經由 Benewake 官方 TFMini Plus 資料表驗證。

### 3.1 封包格式

五支程式採用相同的讀取流程：

1. 等待 `ser.in_waiting >= 9`。
2. 讀取第一個 byte，要求為 `0x59`。
3. 讀取第二個 byte，要求為 `0x59`。
4. 再讀取 7 bytes。

因此它們共同假設固定長度為 9 bytes，資料排列如下：

| 完整封包索引 | 讀完幀頭後的索引 | 程式假設的用途 |
|---:|---:|---|
| 0 | — | 幀頭 `0x59` |
| 1 | — | 幀頭 `0x59` |
| 2 | `raw[0]` / `data[0]` | 距離低位元組 |
| 3 | `raw[1]` / `data[1]` | 距離高位元組 |
| 4 | `raw[2]` / `data[2]` | 訊號強度低位元組 |
| 5 | `raw[3]` / `data[3]` | 訊號強度高位元組 |
| 6 | `raw[4]` / `data[4]` | 溫度低位元組 |
| 7 | `raw[5]` / `data[5]` | 溫度高位元組 |
| 8 | `raw[6]` / `data[6]` | Checksum，但現有程式全部忽略 |

### 3.2 欄位解析

共同採用 little-endian 解析：

```text
distance_cm = payload[0] + payload[1] * 256
strength = payload[2] + payload[3] * 256
temperature_c = (payload[4] + payload[5] * 256) / 8 - 256
```

- `20260713_test.py`、`live_lidar.py`、`pandasTF.py` 與 `tfmini_test.py` 都計算三個欄位。
- `live_lidar2.py` 只計算距離，雖然仍讀取完整的 7-byte 尾段，但未解析訊號強度與溫度。
- `pandasTF.py` 雖然計算訊號強度與溫度，最後只把距離放入 DataFrame 與 CSV，兩個計算結果實際上被丟棄。

### 3.3 Checksum 現況

五支程式都沒有驗證 checksum。它們讀取的最後一個 byte，也就是 `raw[6]` 或 `data[6]`，從未參與任何判斷。

既有程式並未提供 checksum 演算法的來源。若 Benewake 官方 TFMini Plus 通訊協定確認 checksum 是前 8 bytes 總和的低 8 bits，正式實作應先保留完整封包，再使用以下規則驗證：

```text
expected_checksum = sum(packet[0:8]) & 0xFF
is_valid = expected_checksum == packet[8]
```

上述公式目前是待確認的實作候選，不是僅憑舊程式即可證實的規格。正式實作前必須以 Benewake 官方 TFMini Plus 資料表或通訊協定文件確認 checksum 的計算範圍與比較方式。確認後，checksum 驗證應在產生有效量測資料前完成；失敗的封包應被拒絕並交由串流解析器繼續重新同步。

## 四、逐支程式分析

### 4.1 `20260713_test.py`

#### 用途與行為

- 維護最近 50 筆距離並以深色圖表即時顯示。
- 每筆通過距離檢查的資料都先寫入 CSV 的使用者空間緩衝區。
- 每累積 5 筆資料才執行 `flush()` 並更新圖表，意圖降低繪圖及磁碟同步頻率。
- CSV 使用附加模式，記錄時間、距離、訊號強度及溫度；表頭程式碼目前被註解。
- UART 沒有資料時休眠 2 ms，以降低空轉造成的 CPU 使用率。

#### 問題與風險

- `if dist < 10: pass` 不會篩除任何資料，極可能是原本想使用 `continue` 卻留下的無效判斷。
- 未驗證 checksum，雜訊、錯位或損壞封包可能被當成有效量測。
- `ser.read(7)` 可能因逾時回傳少於 7 bytes，後續索引會引發 `IndexError`。
- UART、CSV 與圖形物件都在模組匯入階段初始化。若 UART 開啟後發生 CSV 開檔或圖形初始化錯誤，`main()` 的 `finally` 尚未生效，UART 可能不會關閉。
- `finally` 先執行 `csv_file.flush()`；如果 flush 本身拋出例外，後續的 CSV 與 UART 關閉動作可能不會執行。
- 使用裸露的 `except:` 處理字型設定，會攔截不應忽略的例外。
- 使用廣泛的 `except Exception` 後只印出訊息，沒有保留結構化錯誤資訊，也不利於上層判斷失敗。
- CSV 採附加模式但不主動建立表頭，新檔案可能缺少欄位名稱，舊檔案也可能混入不同格式的資料。
- 解析、篩選、紀錄及 GUI 全部耦合，無法獨立測試 Parser，也使核心流程依賴圖形介面。

### 4.2 `live_lidar.py`

#### 用途與行為

- 使用 `FuncAnimation` 約每 10 ms 呼叫一次更新函式。
- 維護最近 100 筆距離資料。
- CSV 使用寫入模式，每次啟動都覆寫既有的 `lidar_detail_record.csv`，並寫入表頭。
- 每筆有效距離都記錄完整時間戳、距離、訊號強度及溫度，且立即 flush。
- 把距離 `0` 與 `65535` 視為無效值。

#### 問題與風險

- 最大的資源問題是 `finally` 只關閉 CSV，完全沒有呼叫 `ser.close()`；正常關閉視窗或發生例外後，UART 仍可能保持開啟直到程序結束。
- UART 開啟後，CSV 開檔與圖形初始化都發生在 `try/finally` 外；這些步驟失敗時同樣可能洩漏 UART。
- 未驗證 checksum，也未確認 `ser.read(7)` 是否真的取得 7 bytes。
- 每筆資料都 flush，可能造成不必要的儲存裝置寫入負擔。
- CSV 使用 `'w'`，未提示便覆寫既有紀錄。
- 動畫 callback 直接執行 UART、解析及檔案 I/O；任何一步延遲或失敗都會影響 GUI 事件迴圈。
- 模組被 import 時就會開啟 UART、CSV 和視窗，沒有 `if __name__ == "__main__"` 防護。
- GUI、Parser、資料有效性判斷及記錄器互相耦合。

### 4.3 `live_lidar2.py`

#### 用途與行為

- 提供較精簡的即時距離圖，維護最近 100 筆資料。
- 只解析距離，小於 10 cm 的資料直接略過。
- 每五筆被接受的距離才寫入一次 CSV 並 flush；圖表仍會在每筆資料後更新。
- CSV 只包含秒級時間與距離。

#### 問題與風險

- 註解宣稱使用附加模式，但實際呼叫 `open(..., 'w')`，每次執行都會覆寫檔案。
- 沒有排除 `65535`，因此該值會被接受、寫入 CSV，並加入遠超過圖表上限 600 cm 的歷史資料。
- 未解析訊號強度與溫度，無法利用訊號品質協助判斷量測有效性。
- 未驗證 checksum，也未處理短讀。
- UART 在模組頂層直接開啟，沒有處理開啟失敗；CSV 或圖形初始化若在進入 `main()` 前失敗，UART 可能無法釋放。
- `main()` 的 `finally` 會在迴圈結束時關閉 CSV 與 UART，這部分優於 `live_lidar.py`，但保護範圍未涵蓋所有初始化步驟。
- 只特別處理 `KeyboardInterrupt`；其他例外雖會執行 `finally`，但沒有提供具體診斷脈絡。
- `plt.pause(0.001)` 每圈執行，將 UART 輪詢與 GUI 排程綁在一起。

### 4.4 `pandasTF.py`

#### 用途與行為

- 使用 DataFrame 保存最近 50 筆距離資料，以時間戳作為索引。
- 使用 `FuncAnimation` 約每 20 ms 更新一次。
- 每一筆有效距離都以 Pandas `to_csv()` 附加到 CSV。
- 距離 `0` 與 `65535` 會被略過。

#### 問題與風險

- 訊號強度與溫度雖被解析，卻未放入 `new_row`，因此不會記錄或顯示。
- 每筆資料都建立小型 DataFrame、呼叫 `concat()`、開啟或附加 CSV，對高頻感測資料而言成本過高。
- 使用 `pd.io.common.file_exists` 這類 Pandas 內部 API，並非穩定的公開介面，版本更新時可能失效。
- 只以檔案是否存在決定是否寫表頭；既有空檔案可能沒有表頭，既有不同 schema 的檔案也會直接混寫。
- 未驗證 checksum，也未處理短讀。
- UART 在模組匯入階段開啟；其後圖形初始化若失敗，尚未進入包住 `plt.show()` 的 `try/finally`，UART 可能洩漏。
- 模組沒有主程式防護，import 本身就會產生硬體與 GUI 副作用。
- 全域 DataFrame、UART 與圖表物件使測試彼此隔離困難。

### 4.5 `tfmini_test.py`

#### 用途與行為

- 作為最簡單的命令列連線與資料監看工具。
- 連續印出距離、訊號強度與晶片溫度。
- 每次成功解析後休眠 10 ms，接著清空 UART 輸入緩衝區，意圖只保留較新的資料。
- UART 建立在 `try` 內，`finally` 會在 `ser` 已建立時將其關閉，資源清理範圍是五支程式中相對完整的。

#### 問題與風險

- 未驗證 checksum，也未處理短讀。
- 每筆資料後呼叫 `reset_input_buffer()` 會丟棄已經抵達但尚未解析的完整或部分封包，造成資料缺口，不能用於連續資料鏈或取樣率分析。
- 廣泛捕捉所有 `Exception` 後立即結束讀取；暫時性讀取錯誤、單一損壞封包與不可恢復錯誤沒有區分。
- 封包解析仍直接依賴 `serial.Serial`，無法使用純 bytes 單元測試。
- UART 裝置與鮑率硬編碼，註解提到的 `/dev/serial0` 並未做成設定或自動選擇。

## 五、重複程式碼

下列邏輯在多支程式中重複，應抽離為單一且可測試的實作：

1. `/dev/ttyAMA0`、115200 baud 與 timeout 的 UART 設定。
2. `in_waiting >= 9` 後逐 byte 尋找 `0x59 0x59` 的流程。
3. 讀取剩餘 7 bytes，但沒有短讀或 checksum 防護的流程。
4. 距離、訊號強度及溫度的 little-endian 計算。
5. `0`、`65535` 或 `< 10` 等互不一致的距離篩選。
6. `deque` 或 DataFrame 的固定長度歷史資料維護。
7. Matplotlib 深色主題、座標範圍、線條更新及事件迴圈。
8. CSV 時間戳、資料列寫入及 flush。
9. UART 開啟失敗訊息、`KeyboardInterrupt` 與資源關閉處理。

重複不只增加維護成本，也已造成行為分歧，例如 `live_lidar2.py` 未排除 `65535`、不同程式使用不同距離下限、CSV 有的覆寫、有的附加、部分程式完全沒有關閉 UART。

## 六、跨程式的錯誤與設計問題

### 6.1 串流同步不完整

現有程式一次讀取一至兩個幀頭 byte。若第二個 byte 不是 `0x59`，該 byte 即被丟棄；如果它本身是下一個候選資料的一部分，程式沒有保留狀態。正式 Parser 應維護內部緩衝區，能處理：

- 一次只收到部分封包。
- 一次收到多個封包。
- 封包前混入雜訊。
- 幀頭跨越兩次 `read()`。
- 錯誤 checksum 後重新尋找下一個幀頭。
- 資料內容中恰好出現 `0x59 0x59`。

### 6.2 資料有效性規則混亂

部分程式排除 `0` 與 `65535`，部分排除 `< 10`，部分則沒有完整篩選。雖然實際硬體已確認為 TFMini Plus，這些條件目前仍沒有 Benewake 官方 TFMini Plus 資料表或測試資料佐證。Parser 應只負責經官方文件確認的結構與 checksum；量測範圍、訊號品質及應用門檻應由獨立的有效性或濾波模組處理。

### 6.3 資源生命週期不安全

多數程式在模組頂層開啟 UART 或檔案，使 import 產生副作用，且初始化中途失敗時容易洩漏資源。正式實作應使用 context manager 或明確的 `open()`／`close()` 生命週期，並讓最外層應用程式負責清理。清理個別資源時也不應讓第一個 close/flush 例外阻止其他資源釋放。

### 6.4 缺少單一 UART 擁有者

五支程式都能直接開啟同一個 `/dev/ttyAMA0`，沒有程序鎖、單一服務或其他協調機制。若同時啟動兩支程式，可能發生開啟失敗或兩者分食 byte stream。正式系統必須只有一個 UART transport 擁有裝置，再把解析後的量測分派給記錄、監控及警示元件。

### 6.5 錯誤處理與可觀測性不足

- 沒有區分 UART 開啟錯誤、逾時、短讀、封包錯誤、檔案錯誤與 GUI 錯誤。
- 多數程式只 `print()`，沒有結構化 logging、錯誤計數或健康狀態。
- 沒有記錄 checksum 失敗、丟棄 byte、重新同步次數或資料逾時。
- 沒有型別提示，也沒有可供呼叫端處理的明確例外類型。
- 硬編碼 UART、輸出檔名與圖表範圍，不利部署及測試。

## 七、可以保留的邏輯

以下概念可以保留，但應移入正確模組並以測試確認：

1. 舊程式採用的 9-byte、`0x59 0x59` 幀頭、little-endian 欄位及溫度公式，可作為 TFMini Plus Parser 的候選協定邏輯；正式採用前必須逐項對照 Benewake 官方 TFMini Plus 文件及封包樣本。
2. 使用固定長度 `deque` 保存監控畫面的近期資料，適合放在可選的監控層。
3. 沒有 UART 資料時短暫等待的概念，可避免忙碌輪詢；實際方式應由 transport 的阻塞讀取或可控輪詢機制決定。
4. CSV 包含時間、距離、訊號強度與溫度的欄位設計。
5. 批次 flush、降低 GUI 更新頻率的概念，可減少儲存裝置與繪圖負擔，但資料擷取頻率、記錄頻率與顯示頻率應分別設定。
6. 使用 `try/finally` 確保資源釋放，以及使用 `if __name__ == "__main__"` 避免 import 副作用的方向。
7. 對明顯無效量測進行後續篩選的需求，但具體條件必須有協定或實驗依據，且不應混入 Parser。

## 八、應重新設計的邏輯

1. **Parser**：改成只接收 bytes 的純串流 Parser，不得自行建立或讀取 UART。
2. **封包同步**：使用內部 buffer 支援任意切塊、雜訊、多封包、短讀及錯誤後重新同步。
3. **完整性驗證**：驗證封包長度、雙幀頭及 checksum，再建立量測物件。
4. **UART transport**：把 port、baud rate、timeout 設為可注入設定，集中管理開關與唯一擁有權。
5. **資料模型**：使用有型別提示的不可變 dataclass 表示距離、訊號強度、溫度、接收時間及原始封包資訊。
6. **有效性與濾波**：把距離範圍、訊號品質、離群值、平滑與取樣策略移到 Parser 之外。
7. **輸出與監控**：CSV、LED、藍牙語音、終端及 GUI 應是可替換的 consumer；核心警示流程不得依賴 Matplotlib 或 Pandas。
8. **時間處理**：資料記錄可使用含時區的 wall-clock timestamp；逾時、資料新鮮度與碰撞演算法的時間差應使用 monotonic clock。
9. **錯誤策略**：定義可預期例外、重試及停止條件，並記錄封包錯誤率、最後成功資料時間和 UART 健康狀態。
10. **測試**：以固定 bytes fixture 驗證正常、checksum 錯誤、雜訊、分段輸入、連續封包、極端欄位及重新同步，不需要實體 UART。

## 九、硬體型號與規格確認狀態

### 9.1 使用者已確認的資訊

本專案實際使用的距離感測器已由使用者確認為 **Benewake TFMini Plus**，不是一般 TFMini，也不是 TFMini-S。後續正式設計、文件與測試應以 TFMini Plus 為唯一目標型號，除非使用者另行變更硬體範圍。

### 9.2 從既有程式觀察到的行為與參數

舊程式共同呈現下列行為：

- 使用 `/dev/ttyAMA0`。
- 使用 115200 baud。
- 等待至少 9 bytes，並以 `0x59 0x59` 尋找幀頭。
- 以 little-endian 解析距離、訊號強度及溫度原始值。
- 使用 `(raw_temperature / 8) - 256` 計算攝氏溫度。
- 未驗證第 9 個 byte 的 checksum。

這些項目是對舊程式的客觀描述。部分程式的註解與圖表標題寫有「TFmini Plus」，其內容與使用者確認的型號一致，但檔名、註解及標題本身不作為硬體型號或官方規格的證明。

### 9.3 尚需官方 TFMini Plus 資料表確認的規格

在正式 Parser、UART transport 與有效性判斷實作前，應以 Benewake 官方 TFMini Plus 資料表或通訊協定文件確認：

1. UART 預設鮑率、可設定範圍、資料位元、同位元及停止位元。
2. 量測輸出封包是否固定為 9 bytes，以及 `0x59 0x59` 的正式定義。
3. Checksum 所涵蓋的 bytes、計算方法及錯誤封包處理要求。
4. 距離、訊號強度與溫度欄位的位置、位元序、單位及溫度轉換公式。
5. `0`、`65535`、小於 10 cm 等數值是否具有正式的無效、超量程或特殊狀態意義。
6. 有效量測範圍、精度、解析度、訊號強度或可靠度門檻。
7. 預設及可設定的資料輸出頻率。
8. 裝置設定、版本查詢或產品資訊命令的格式與回應。

在完成官方文件核對前，上述舊程式參數只能作為候選設定與測試案例，不應被描述為已確認的 TFMini Plus 規格。

### 9.4 型號推論限制

不得因 `tfmini_test.py` 等通用檔名推論硬體是一般 TFMini，也不得因任何歷史註解推論是 TFMini-S 或其他版本。硬體型號以使用者已確認的 Benewake TFMini Plus 為準；舊程式只用於分析既有軟體行為。

## 十、建議的正式 Python Package 結構

```text
pyproject.toml
src/
└── rear_collision_warning/
    ├── __init__.py
    ├── config.py
    ├── app.py
    ├── tfmini/
    │   ├── __init__.py
    │   ├── models.py
    │   ├── parser.py
    │   ├── protocol.py
    │   ├── transport.py
    │   └── reader.py
    ├── filtering/
    │   ├── __init__.py
    │   └── distance.py
    ├── warning/
    │   ├── __init__.py
    │   └── policy.py
    ├── outputs/
    │   ├── __init__.py
    │   ├── base.py
    │   ├── led.py
    │   └── bluetooth_audio.py
    ├── recording/
    │   ├── __init__.py
    │   └── csv_recorder.py
    └── monitoring/
        ├── __init__.py
        └── console.py
tests/
├── unit/
│   ├── tfmini/
│   │   ├── test_parser.py
│   │   └── test_protocol.py
│   ├── filtering/
│   │   └── test_distance.py
│   └── warning/
│       └── test_policy.py
├── integration/
│   └── test_tfmini_reader.py
├── fixtures/
│   └── tfmini_packets.py
└── fakes/
    └── fake_byte_stream.py
```

### 模組責任

- `tfmini/models.py`：定義有型別的量測結果與解析狀態。
- `tfmini/protocol.py`：保存協定常數、欄位位置及 checksum 函式，不執行 I/O。
- `tfmini/parser.py`：接收任意 bytes、維護 buffer、重新同步並輸出已驗證量測，不依賴 PySerial。
- `tfmini/transport.py`：封裝唯一的實體 UART 存取及生命週期；可定義抽象 byte-stream 介面供測試替換。
- `tfmini/reader.py`：協調 transport 與 Parser，但不執行濾波、警示或 GUI。
- `filtering/`：處理量測有效性、訊號品質、平滑與離群值。
- `warning/`：只根據已處理資料產生警示狀態，不依賴 GUI 或實體輸出裝置。
- `outputs/`：實作 LED 與藍牙語音等輸出，透過共同介面接收警示狀態。
- `recording/`：負責批次、flush、檔案輪替與檔案錯誤處理。
- `monitoring/`：提供可選監控介面；圖形監控若日後需要，應另建模組及可選相依套件，不得成為核心執行需求。
- `tests/fixtures/` 與 `tests/fakes/`：提供確定性封包及模擬 byte stream，確保 Parser 和 reader 測試不會開啟實體 UART。

此結構直接對應 `TFMini UART 通訊 → 位元組串流與封包解析 → 有效量測 → 濾波、記錄、監控與警示` 的分層，同時確保 UART 硬體介面、Parser、濾波、警示策略及輸出裝置彼此獨立。
