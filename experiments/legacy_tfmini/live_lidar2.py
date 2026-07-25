import serial
import time
import csv
import matplotlib.pyplot as plt
from collections import deque

# --- 參數設定 ---
UART_PORT = "/dev/ttyAMA0"
BAUD_RATE = 115200
HISTORY_SIZE = 100 
SAVE_FILE = "lidar_record.csv"

# 初始化數據
dist_history = deque([0]*HISTORY_SIZE, maxlen=HISTORY_SIZE)
ser = serial.Serial(UART_PORT, BAUD_RATE, timeout=0.01) # 縮短 timeout

# 設定圖表 (移除中文避免報錯，提升效能)
plt.style.use('dark_background')
fig, ax = plt.subplots()
line, = ax.plot(range(HISTORY_SIZE), dist_history, color='#00ff00', lw=1.5)
ax.set_ylim(0, 600)
ax.set_title("TFmini Plus Real-time")
plt.grid(True, alpha=0.3)

# 準備 CSV 檔案（使用 'a' 模式並手動 flush）
csv_file = open(SAVE_FILE, 'w', newline='')
writer = csv.writer(csv_file)
writer.writerow(["Time", "Distance"])

def main():
    try:
        count = 0
        while True:
            if ser.in_waiting >= 9:
                if ser.read(1) == b'\x59':
                    if ser.read(1) == b'\x59':
                        raw = ser.read(7)
                        dist = raw[0] + raw[1] * 256
                        
                        if dist < 10: continue

                        dist_history.append(dist)
                        
                        # 每 5 筆數據才寫入一次硬碟，減少 I/O 負擔
                        count += 1
                        if count % 5 == 0:
                            writer.writerow([time.strftime("%H:%M:%S"), dist])
                            csv_file.flush() # 強制寫入硬碟

                        # 快速更新圖表
                        line.set_ydata(dist_history)
                        fig.canvas.draw()
                        fig.canvas.flush_events()
                        
            # 關鍵：減少 plt.pause 的等待時間或改用 flush_events
            plt.pause(0.001) 

    except KeyboardInterrupt:
        print("停止中...")
    finally:
        csv_file.close()
        ser.close()

if __name__ == "__main__":
    main()
