import serial
import time
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

# --- 1. 設定與初始化 ---
UART_PORT = "/dev/ttyAMA0"
BAUD_RATE = 115200
HISTORY_SIZE = 50 
SAVE_FILE = "lidar_detail_record_05_25_indoor.csv"

# 初始化一個空的 DataFrame 來存放顯示用的數據
# columns: ['Time', 'Distance', 'Strength', 'Temp']
df_display = pd.DataFrame(columns=['Distance'])

try:
    ser = serial.Serial(UART_PORT, BAUD_RATE, timeout=0.01)
except Exception as e:
    print(f"串口開啟失敗: {e}")
    exit()

# 設定繪圖風格
plt.style.use('dark_background')
fig, ax = plt.subplots()
ax.set_ylim(0, 1000)
ax.set_title("TFmini Plus 實時距離監測 (Pandas 模式)")
ax.set_ylabel("距離 (cm)")
line, = ax.plot([], [], color='#00FFCC', lw=2)

# --- 2. 核心數據處理函數 ---
def update_data(frame):
    global df_display
    
    # 讀取串口數據 (維持原本的 9 bytes 協議)
    if ser.in_waiting >= 9:
        if ser.read(1) == b'\x59':
            if ser.read(1) == b'\x59':
                raw = ser.read(7)
                dist = raw[0] + raw[1] * 256
                strength = raw[2] + raw[3] * 256
                temp = (raw[4] + raw[5] * 256) / 8 - 256
                
                # 過濾異常值
                if dist == 0 or dist == 65535: return line,

                # 獲取時間戳
                ts = pd.Timestamp.now()
                
                # 建立新資料行
                new_row = pd.DataFrame({'Distance': [dist]}, index=[ts])
                
                # 寫入 CSV (直接附加模式)
                # 使用 pandas 的 to_csv 比手動 writer 更簡潔
                new_row.to_csv(SAVE_FILE, mode='a', header=not pd.io.common.file_exists(SAVE_FILE))
                
                # 更新顯示用的 DataFrame
                df_display = pd.concat([df_display, new_row]).iloc[-HISTORY_SIZE:]

    # 更新圖表線條
    if not df_display.empty:
        line.set_data(range(len(df_display)), df_display['Distance'])
        ax.set_xlim(0, HISTORY_SIZE)
        
    return line,

# --- 3. 執行主程式 ---
try:
    # 使用 FuncAnimation 取代手動 while 迴圈，效率更高且不卡頓
    # interval: 毫秒，代表更新頻率
    ani = FuncAnimation(fig, update_data, interval=20, blit=True, cache_frame_data=False)
    print(f"開始紀錄與繪圖... 數據儲存至: {SAVE_FILE}")
    plt.show()

except KeyboardInterrupt:
    print("\n使用者停止程式")
finally:
    ser.close()
    print("串口已關閉")
