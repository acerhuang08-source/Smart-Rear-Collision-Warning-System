import serial
import time

# 樹莓派 5 預設硬體串口
# 如果這行報錯，請嘗試 "/dev/serial0"
UART_PORT = "/dev/ttyAMA0"
BAUD_RATE = 115200

def read_tfmini():
    try:
        ser = serial.Serial(UART_PORT, BAUD_RATE, timeout=0.1)
        print(f"正在連線至 {UART_PORT}...")
        
        while True:
            # TFmini Plus 的數據包長度為 9 bytes
            if ser.in_waiting >= 9:
                # 尋找幀頭 0x59
                if ser.read(1) == b'\x59':
                    # 確認第二個字節也是 0x59
                    if ser.read(1) == b'\x59':
                        # 讀取剩下的 7 個字節
                        data = ser.read(7)
                        
                        # 解析數據 (Little Endian 低位在前)
                        # data[0]: Dist_L, data[1]: Dist_H
                        distance = data[0] + data[1] * 256
                        
                        # data[2]: Strength_L, data[3]: Strength_H
                        strength = data[2] + data[3] * 256
                        
                        # data[4]: Temp_L, data[5]: Temp_H (攝氏溫度 = Temp / 8 - 256)
                        temp = (data[4] + data[5] * 256) / 8 - 256
                        
                        print(f"距離: {distance:3} cm | 強度: {strength:5} | 晶片溫度: {temp:.1f} °C")
                        
                        # 稍微延遲並清除緩衝區，確保抓到的是最新的數據
                        time.sleep(0.01)
                        ser.reset_input_buffer()
                        
    except Exception as e:
        print(f"發生錯誤: {e}")
    finally:
        if 'ser' in locals():
            ser.close()

if __name__ == "__main__":
    read_tfmini()
