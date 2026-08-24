import serial
import time
import csv
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from collections import deque

# --- 參數設定 ---
UART_PORT = "/dev/ttyAMA0"
BAUD_RATE = 115200
HISTORY_SIZE = 100 
SAVE_FILE = "lidar_detail_record.csv"

# 設定中文與樣式
plt.rcParams['font.sans-serif'] = ['WenQuanYi Micro Hei']
plt.rcParams['axes.unicode_minus'] = False
plt.style.use('dark_background')

dist_history = deque([0]*HISTORY_SIZE, maxlen=HISTORY_SIZE)

# 初始化串口
try:
    ser = serial.Serial(UART_PORT, BAUD_RATE, timeout=0.1)
except Exception as e:
    print(f"無法開啟串口: {e}")
    exit()

# --- 即時儲存優化：預先開啟檔案並寫入表頭 ---
csv_file = open(SAVE_FILE, 'w', newline='', buffering=1) # buffering=1 表示行緩衝
writer = csv.writer(csv_file)
writer.writerow(["Timestamp", "Distance_cm", "Strength", "Temperature_C"])

# 設定圖表
fig, ax = plt.subplots()
line, = ax.plot(range(HISTORY_SIZE), dist_history, lw=2, color='#00FFCC')
ax.set_ylim(0, 1200) # TFmini Plus 範圍可達 12m (1200cm)
ax.set_title("TFmini Plus 實時距離監測 (詳細模式)")
ax.set_ylabel("距離 (cm)")
ax.set_xlabel("最近數據點")
plt.grid(True, linestyle='--', alpha=0.3)

def update(frame):
    if ser.in_waiting >= 9:
        # 尋找幀頭 0x59 0x59
        if ser.read(1) == b'\x59':
            if ser.read(1) == b'\x59':
                data = ser.read(7)
                
                # 解析數據 (依據 TFmini Plus 通訊協定)
                dist = data[0] + data[1] * 256          # 距離 (cm)
                strength = data[2] + data[3] * 256      # 信號強度
                temp_raw = data[4] + data[5] * 256      # 晶片溫度
                temp_c = temp_raw / 8 - 256             # 轉換為攝氏度
                
                # 排除無效值 (0 或 65535 通常代表異常或超出範圍)
                if dist == 0 or dist == 65535:
                    return line,

                # 更新繪圖容器
                dist_history.append(dist)
                
                # --- 詳細記錄與即時強制寫入 ---
                current_time = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"
                writer.writerow([current_time, dist, strength, f"{temp_c:.2f}"])
                csv_file.flush() # 強制將緩衝區數據寫入硬碟

                # 更新畫線
                line.set_ydata(dist_history)
    
    return line,

try:
    ani = animation.FuncAnimation(fig, update, interval=10, blit=True, cache_frame_data=False)
    plt.show()
finally:
    # 確保程式關閉時檔案會正常關閉
    csv_file.close()
    print(f"數據已完整儲存至 {SAVE_FILE}")
