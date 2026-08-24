import serial
import time
import csv
import matplotlib.pyplot as plt
from collections import deque

# --- 1. 中文與字體設定 ---
try:
    plt.rcParams['font.sans-serif'] = ['WenQuanYi Micro Hei']
    plt.rcParams['axes.unicode_minus'] = False
except:
    print("提示：未偵測到中文字體，標題可能顯示為空格")

# --- 2. 參數設定 ---
UART_PORT = "/dev/ttyAMA0"
BAUD_RATE = 115200
HISTORY_SIZE = 50   
SAVE_FILE = "lidar_detail_record.csv"
GUI_UPDATE_RATE = 5  # 【新增】每收到 5 筆數據才更新一次圖表與寫入實體檔案 (將 100Hz 降為 20Hz 渲染)

# 初始化固定長度的數據容器
dist_history = deque([0]*HISTORY_SIZE, maxlen=HISTORY_SIZE)

# 初始化串口
try:
    ser = serial.Serial(UART_PORT, BAUD_RATE, timeout=0.01)
except Exception as e:
    print(f"串口開啟失敗: {e}")
    exit()

# --- 3. CSV 即時寫入優化  ---
csv_file = open(SAVE_FILE, 'a', newline='') # 移除 buffering=1，交給系統底層優化
writer = csv.writer(csv_file)
# writer.writerow(["Timestamp", "Distance_cm", "Strength", "Temp_C"]) 

# 設定繪圖視窗
plt.style.use('dark_background') 
fig, ax = plt.subplots()
line, = ax.plot(range(HISTORY_SIZE), dist_history, color='#00FFCC', lw=2)
ax.set_ylim(0, 1000) 
ax.set_title("TFmini Plus 實時距離監測 (詳細模式)")
ax.set_ylabel("距離 (cm)")
ax.set_xlabel("最近數據點")
plt.grid(True, alpha=0.3)

def main():
    print(f"程式開始執行，數據將即時儲存至 {SAVE_FILE}")
    update_counter = 0 # 【新增】計數器
    
    try:
        while True:
            if ser.in_waiting >= 9:
                if ser.read(1) == b'\x59':
                    if ser.read(1) == b'\x59':
                        raw = ser.read(7)
                        
                        dist = raw[0] + raw[1] * 256
                        strength = raw[2] + raw[3] * 256
                        temp = (raw[4] + raw[5] * 256) / 8 - 256
                        
                        if dist == 0 or dist == 65535: continue
                        if dist < 10: 
                            pass 

                        # 1. 更新數據容器
                        dist_history.append(dist)
                        
                        # 2. 獲取精確時間戳 
                        current_time = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"
                        
                        # 3. 將數據寫入記憶體緩衝區 (不強迫立刻寫入 SD 卡)
                        writer.writerow([current_time, dist, strength, f"{temp:.2f}"])
                        
                        # 4. 【關鍵修改】限制繪圖與磁碟寫入頻率
                        update_counter += 1
                        if update_counter >= GUI_UPDATE_RATE:
                            # 批量寫入 SD 卡
                            csv_file.flush() 
                            
                            # 更新圖表
                            line.set_ydata(dist_history)
                            fig.canvas.draw_idle()
                            fig.canvas.flush_events()
                            
                            # 給予系統處理介面事件的時間
                            plt.pause(0.001)
                            
                            # 重置計數器
                            update_counter = 0
            else:
                # 【關鍵修改】當緩衝區沒有資料時，強迫 CPU 休息 2 毫秒，防止死循環吃滿 CPU
                time.sleep(0.002)

    except KeyboardInterrupt:
        print("\n使用者停止程式")
    except Exception as e:
        print(f"\n發生錯誤: {e}")
    finally:
        csv_file.flush() # 確保最後一筆資料有被寫入
        csv_file.close()
        ser.close()
        print(f"檔案已安全關閉。路徑：{SAVE_FILE}")

if __name__ == "__main__":
    main()
