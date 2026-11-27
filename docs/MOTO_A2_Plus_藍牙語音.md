# MOTO A2 Plus 藍牙語音模組

首次實作：2026-11-01；語音素材放置：2026-11-08，Asia/Taipei

## 2026-11-08 自連設定後邊界

MOTO A2 Plus 開機自連已以獨立 `topics` user service 安裝，不使用語音 dispatcher，也不將
`enabled` 改為 true。自連只管 BlueZ connection 與動態唯一 A2DP `Audio/Sink` 就緒；播放仍會
另行使用 `node.dont-fallback`、`node.dont-reconnect`、`node.dont-move`。這三個 stream properties
不是裝置自連禁止開關。現地設定、開機待驗情境與還原方式見
`docs/開機藍牙自連與感測儀表板_2026-11-08.md`。

開機實測中，耳機先開時於 boot 後 9.152 秒建立唯一 sink；Pi 先開、耳機後開
時，自連服務從 disconnected 到 `audio_ready` 為 30.001 秒，通過本輪 90 秒界線。
全程沒有掃描、手動 connect 或播放，`Discovering=no`、`enabled=false`。本次 boot
已有 Wayland session，未另做純無桌面 boot 實景。

## 2026-11-08 首次藍牙語音播放實驗（首句未通過後停止）

操作員確認後，以新增的獨立工具 `tools/voice_playback_experiment.py` 執行首次播放。
工具只讀取被 `.gitignore` 排除的私有設定，沿用既有 backend 核對耳機身分與
設定指定的唯一 PipeWire `Audio/Sink`；沒有寫死節點編號。播放仍使用明確 target 與：

- `node.dont-fallback=true`
- `node.dont-reconnect=true`
- `node.dont-move=true`
- 單次串流 `--volume=0.25`

播放前確認 PipeWire、pipewire-pulse 與 WirePlumber user services 均為 active，MOTO A2 Plus
已配對且已連線，設定指定的 MOTO `Audio/Sink` 唯一且可用。沒有掃描、配對、
重新連線、切換預設輸出、調整全域音量或啟停服務。

| 順序 | 音檔／預期文字 | 執行狀態 | 從 MOTO 聽到 | 內容正確清楚、無漏字或截斷 | 音量 | 卡頓 | 結論 |
| ---: | --- | --- | --- | --- | --- | --- | --- |
| 1 | `rear_distance_attention.wav`／「請留意後方距離」 | `pw-play` 退出成功 | 是 | 否 | 太小 | 是 | 未通過；立即停止 |
| 2 | `rear_distance_too_close.wav`／「注意後方，距離過近」 | 未播放 | 未驗證 | 未驗證 | 未驗證 | 未驗證 | 未執行 |
| 3 | `sensor_fault.wav`／「感測資料異常」 | 未播放 | 未驗證 | 未驗證 | 未驗證 | 未驗證 | 未執行 |

操作員回報首句確實由 MOTO A2 Plus 發聲，但內容不正確清楚／有漏字或截斷、音量太小，
並且有卡頓。依任務停止條件沒有自動重播、調高音量或切換裝置，第二與第三句均未播放。
工具以失敗狀態正常退出，收尾盤點沒有殘留 `pw-play` 程序。

本次只對新工具執行語法編譯與 `--help` 檢查；沒有完整回歸、斷線測試、道路實驗或感測器
重測。`config/voice-alert.local.toml` 全程維持 `enabled=false`，沒有接入 SHADOW 或正式偵測流程。

## 2026-11-08 中文語音素材放置

- 素材包 `MOTO_A2_Plus_中文語音素材_2026-11-07.zip` 已先解壓至專案外的暫存目錄，沒有
  直接在專案內展開或覆寫既有檔案。
- 三個 WAV 的檔名、大小與 SHA-256 均與素材包 `PROVENANCE.json` 一致，已放入
  `assets/voice/`；放置前專案內沒有同名 WAV，因此沒有檔案衝突或覆寫。
- 素材來源紀錄已原樣保存為 `assets/voice/PROVENANCE.json`。沒有以素材包內的
  README 覆寫本文件或其他現地文件。

| 檔名 | 大小 | SHA-256 |
| --- | ---: | --- |
| `rear_distance_attention.wav` | 191748 bytes | `f962b3c10e3e0c37666b00b2552e368ea20e4841b98e8475d4e62bbb286682e7` |
| `rear_distance_too_close.wav` | 232400 bytes | `defb13d689d2429f1a63d32f99cabef75b47fb54259e794c856b54350ec4eaf7` |
| `sensor_fault.wav` | 166714 bytes | `0b60db9f7df4fc6ff608bba105850e7ab9a304f404cfcda2b2bba75855292016` |

`assets/voice/prompts.toml` 原有的 schema、文字、事件與檔名對映已正確；本次只將三個
`present` 狀態更新為 `true`。`config/voice-alert.local.toml` 的私有耳機識別資料未改，
`enabled=false` 仍保持。

本次沒有試聽、播放、啟動語音程式、執行測試，也沒有接入 SHADOW 或正式偵測流程。
檔案已就位只代表靜態素材完整，不代表發音、音量、藍牙路由或實際可聽性已驗證。

## 2026-11-01 實作邊界

本輪只新增獨立、預設關閉的語音輸出模組，沒有接入或改動現有 ioRadar701 SHADOW
入口、距離／融合門檻、GPIO、BNO055 校正或感測器讀取。import、載入設定、建立 backend
與 dispatcher 都不會掃描、配對、連線或播音；只有呼叫者明確設定 `enabled=true`、建立
完整設定、呼叫 `start()` 並提交事件後，單一 worker 才可能檢查路由與播放。

本輪遵照任務書沒有執行單元測試、模擬、回放、dry-run、編譯檢查或試播。程式僅做
人工靜態審閱，因此不能宣稱語音可聽見、音量合適、延遲達標或騎乘環境可辨識。

## 2026-11-01 現地盤點（歷史）

取得操作員授權後，已完成服務、session owner、配對、連線與 Audio/Sink 狀態
盤點；沒有播放音訊：

| 項目 | 現地結果 | 判讀 |
| --- | --- | --- |
| 使用者 | `topics`（uid 1000）；active Wayland session 2 位於 seat0 | SSH 與桌面 session 不同，但共用 uid 1000 的 user audio services。 |
| BlueZ | 5.82；system service active/running；指定 adapter powered、pairable | 初始已知 device 清單為空；限時 discovery 與連線完成後已確認 `Discovering: no`。 |
| PipeWire | 1.4.2；`pipewire`、`pipewire-pulse` user services active/running | 初始只有 `auto_null`；連線後已建立唯一 MOTO Bluetooth Audio/Sink。 |
| WirePlumber | 0.5.8 user service active/running | client 屬於 `topics`；local linking scripts 支援 fail-closed properties。 |
| PulseAudio | `pulseaudio`／`pulseaudio-utils` 未安裝；無 `pactl`／`paplay` | 不新增 PulseAudio 相依，採 PipeWire backend。 |
| 耳機身分 | 私有設定指定的 MOTO A2 Plus（識別資料遮罩） | Paired、Bonded、Trusted、Connected 均為 yes；配對時 ServicesResolved=yes。 |
| 音訊 sink | 私有設定對應的動態唯一 sink（節點名遮罩） | 唯一 `Audio/Sink`，bluez5、A2DP sink、SBC。 |
| 音檔 | 當時三檔都缺少 | 2026-11-08 已放入並靜態核對；仍未試播。 |

官方產品頁記載 MOTO A2 Plus 使用藍牙 5.2 並支援同時連接兩台裝置。BlueZ 的
`Pair()` 會配對並取得服務資料，`Connect()` 是另一個連線動作；因此文件與程式把
paired、connected、PipeWire audio sink available 分開，不把其中一項冒充另一項。
WirePlumber 的 Bluetooth monitor 預設受 logind active session 管理。2026-11-08 為支援
linger 無桌面使用，已依現地 WirePlumber 0.5.8 語法安裝 `topics` 專用
seat-monitoring override；不改 root 或其他使用者音訊實例。同機其他圖形使用者可能發生
Bluetooth 音訊所有權競爭，設定與還原步驟見自連儀表板文件。

參考：

- <https://www.id221.com.tw/product/moto-a2-plus/>
- <https://bluez.readthedocs.io/en/latest/device-api/>
- <https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/bluetooth.html>
- <https://docs.pipewire.org/1.4/page_man_pw-cat_1.html>

## 程式結構

```text
既有已確認 WarningState
  -> VoiceReminderDispatcher.submit_warning()（短鎖、立即返回）
  -> 有上限 pending queue／去重／冷卻／TTL／優先權
  -> 唯一 voice-reminder-player worker
  -> BlueZ 指定 MAC 狀態 + PipeWire 指定 Audio/Sink 狀態
  -> pw-play --target=<明確 MOTO node> <真實 WAV>
```

- `models.py`：事件、政策、提交結果、BlueZ／audio 分離狀態及非同步 delivery report。
- `dispatcher.py`：預設 disabled；顯式 `start()`；單一播放 worker。感測／融合 caller
  只做短暫 queue 操作，不等待狀態查詢或音檔播放。
- `backend.py`：只接受完整 MOTO MAC 與非 `auto` 的明確 target。每次播放前重新核對
  BlueZ paired／connected、Name／Alias 及唯一 Bluetooth `Audio/Sink`；不符合即失敗，
  不會改播 HDMI、analog 或其他裝置。播放 stream 同時設定
  `node.dont-fallback=true`、`node.dont-reconnect=true`、`node.dont-move=true`。
- `config.py`：以標準庫 `tomllib` 載入設定。設定 disabled 時允許保留 placeholder；
  enabled 時拒絕未填 MAC／target。載入設定與 build dispatcher 都不執行外部命令。

## 事件與語音

| 既有事件 | 語音事件 | 文字 | 預設行為 |
| --- | --- | --- | --- |
| `WarningState.WARNING` | `distance_attention` | 請留意後方距離 | 播放（有新鮮事件且路由可用時） |
| `WarningState.DANGER` | `distance_too_close` | 注意後方，距離過近 | 最高優先，可取代等待中的低優先提醒 |
| `WarningState.SENSOR_FAULT` | `sensor_fault` | 感測資料異常 | 高於一般距離提醒，不能轉成 SAFE |
| `WarningState.SAFE` | 無 | 無 | 預設忽略、不播 |
| `UNCERTAIN`、未知值、無效資料 | 無 | 無 | 回報 `unsupported_state`，不產生安全提示 |

音檔清單在 `assets/voice/prompts.toml`。2026-11-08 已放入：

```text
assets/voice/rear_distance_attention.wav
assets/voice/rear_distance_too_close.wav
assets/voice/sensor_fault.wav
```

缺檔會產生 `audio_file_missing` report，不會建立空白檔或假稱播放成功。

2026-11-01 現地沒有可用的離線中文 TTS 或既有音檔，當時因此不生成佔位檔。
2026-11-08 放入的三檔來自 Windows 離線生成素材包，引擎、版本、來源 commit、合成參數與
檔案雜湊記錄於 `assets/voice/PROVENANCE.json`；Pi 上沒有重新生成音檔。

## 明確 target 的失效保護

單獨使用 `pw-play --target` 不足：WirePlumber 預設在 defined target 不存在時 fallback
到 default／best target，target 消失後也會嘗試 reconnect。現地 WirePlumber 0.5.8
已由安裝檔直接核對：

```text
/usr/share/wireplumber/scripts/linking/find-defined-target.lua
  讀取 node.dont-fallback 與 node.dont-move
/usr/share/wireplumber/scripts/linking/prepare-link.lua
  讀取 node.dont-reconnect
/usr/share/wireplumber/scripts/lib/linking-utils.lua
  讀取 node.dont-reconnect
```

現地 PipeWire 1.4.2 的 `pw-cat(1)` man page也明記 `-P | --properties=VALUE` 可設定 JSON
stream properties。因此實作命令固定等價於：

```text
pw-play \
  --target=<唯一 MOTO Audio/Sink node.name 或 object.serial> \
  --properties={"node.dont-fallback":true,"node.dont-reconnect":true,"node.dont-move":true} \
  --media-category=Playback --media-role=Notification <WAV>
```

播放前仍以 `pw-dump` 要求 target 恰好匹配一個 `Audio/Sink`、屬於 bluez5 且 MAC 相符；
若 target 在檢查後消失，上述 properties 會令播放失敗，而不是重接或 fallback。官方
語意：<https://pipewire.pages.freedesktop.org/wireplumber/policies/linking.html>。

## 提醒控制語意

- 相同 `deduplication_key` 已等待時拒絕重複；接受後預設 8 秒冷卻。
- 事件預設 3 秒有效；提交時或輪到播放前已過期就丟棄。
- pending queue 預設最多 4 筆。較高優先事件會移除等待中的較低優先事件；已經開始的
  一次播放不被另一個 thread 中斷，因為永遠只有一個播放者。
- backend unavailable／耳機斷線／sink 消失時，當前事件明確失敗並清空全部待播；同時
  清除這批事件的 cooldown 記錄，讓恢復後的「新事件」可立即進入，但舊事件不補播。
- `close()` 清空 pending 並要求 worker 結束；播放 subprocess 有 timeout，不會阻塞
  感測 thread，但 shutdown timeout 不能保證立即終止已卡在 kernel 的外部程序。

## 設定範例

範例：`config/voice-alert.example.toml`。它固定 `enabled=false` 並保留 MAC／target
placeholder。本機實際識別資料已保存於被 `.gitignore` 排除的
`config/voice-alert.local.toml`，同樣維持 `enabled=false`；不要把本機 MAC 誤當成通用
值，也不要將 target 設成 `auto`。

```python
from pathlib import Path

from rear_warning.voice import build_voice_dispatcher, load_voice_settings

settings = load_voice_settings(Path("config/voice-alert.local.toml"))
voice = build_voice_dispatcher(settings)  # 不連線、不播音
voice.start()                             # 只啟動 idle worker

# 由未來明確整合點提交「已確認」的 WarningState：
# result = voice.submit_warning(state, occurred_at_monotonic=timestamp)

# finally:
#     voice.close()
```

本輪沒有把這段接入 SHADOW 或正式感測流程；語音仍為獨立 opt-in output。

## 必要連線盤點與設定（已完成，未試播）

依現地 `AGENTS.md`，以下藍牙／systemd 命令已先取得操作員確認。本輪不試播。

1. 已完成唯讀查 BlueZ、使用者音訊服務與已知設備；實際結果見上表：

   ```bash
   systemctl is-active bluetooth.service
   systemctl --user is-active pipewire.service pipewire-pulse.service wireplumber.service
   bluetoothctl show
   bluetoothctl devices
   wpctl status --name
   ```

2. 初始已知設備沒有 MOTO A2 Plus。操作員將耳機切入配對模式後，完成兩次各不超過
   30 秒的 discovery：第一輪辨識唯一名稱吻合 MAC；因 transient device object 在
   client 結束後消失，第二輪在 discovery 有效時完成配對。兩輪結束均確認
   `Discovering: no`。其他掃描裝置完全未操作：

   ```bash
   bluetoothctl --timeout 30 scan on
   bluetoothctl scan off
   ```

3. 只對私有設定辨識的唯一 MOTO 完成 pair／trust／connect，沒有 remove 其他
   配對、沒有試播：

   ```bash
   bluetoothctl pair <私有設定中的-MOTO-MAC>
   bluetoothctl trust <私有設定中的-MOTO-MAC>
   bluetoothctl connect <私有設定中的-MOTO-MAC>
   bluetoothctl info <私有設定中的-MOTO-MAC>
   wpctl status --name
   ```

4. 2026-11-01 建立 sink 時現地已有 active Wayland seat0 session；2026-11-08 後續任務
   已另外安裝 `topics` 專用 WirePlumber 0.5 seat-monitoring override 與專案 user unit，
   用來支援 linger 啟動及有界自連。不改其他使用者設定。

連線後 `wpctl` 顯示該 MOTO sink 前有 `*` 且 volume 0.80；這是 WirePlumber 的自動
選擇結果，本輪沒有執行 `wpctl set-default`、沒有改全域／device 音量，也沒有播放。
文件收尾前的最後一次唯讀快照仍為 Connected=yes、`Discovering: no`，且
Audio/Sink 仍為上述 node.name／object.serial 79。

## 尚待完成／驗證

- Bluetooth paired／bonded／trusted／connected 與 PipeWire Audio/Sink 都已確認；首句也已
  實際從耳機發聲，但聽感未通過。「Connected=yes」、「Audio/Sink 已建立」與程式退出成功
  都不等於語音驗證通過。
- `config/voice-alert.local.toml` 仍為 disabled；三個 WAV 已就位，但不因檔案存在就啟用。
- 首句已實際從 MOTO A2 Plus 播放，但內容清晰度／完整性未通過、音量太小且有卡頓；第二與
  第三句未播放。三句均不能視為已驗收。
- 只完成新手動工具的語法與 `--help` 檢查；未執行完整回歸、型別檢查，也未驗證 queue
  concurrency、斷線、冷卻、TTL、target
  fail-closed 或 cleanup 的 runtime 行為。啟用前仍需另案獲准後測試。
- 未接入現有 SHADOW／live；沒有道路、帽內可聽度、延遲或與手機雙連混音驗證。
