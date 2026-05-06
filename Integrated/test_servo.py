import serial
import time

def test_servos(port="COM5"):
    try:
        ser = serial.Serial(port, 9600, timeout=1)
        time.sleep(3) # Wait for Arduino to reset
        
        print("Testing Pin 8 (Left Hand / s1)...")
        ser.write(b"$090090090")
        time.sleep(1)
        ser.write(b"$180090090")
        time.sleep(2)
        
        print("Testing Pin 9 (s2 - currently assigned to Head)...")
        ser.write(b"$180090090")
        time.sleep(1)
        ser.write(b"$180180090")
        time.sleep(2)
        
        print("Testing Pin 10 (s3 - currently assigned to Right Hand)...")
        ser.write(b"$180090090")
        time.sleep(1)
        ser.write(b"$180090180")
        time.sleep(2)
        
        # Reset all to safe positions
        ser.write(b"$180090000")
        ser.close()
        print("Test complete.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_servos()
