#----------------- Import Libraries -----------------#
import vosk
import pyaudio
import json
import pygame
import pyttsx3
import os
import threading
from datetime import datetime
from flask import Flask, jsonify, render_template, request
from groq import Groq
from aminulzone.aminulzone.SerialModule import SerialObject
from time import sleep, monotonic

# ----------------- Initialize Modules -----------------#

def initialize_arduino(preferred_port="COM5"):
    # Try preferred port first, then fallback to auto-detection.
    serial_obj = None
    try:
        serial_obj = SerialObject(portNo=preferred_port, digits=3)
    except Exception:
        serial_obj = None

    if hasattr(serial_obj, "ser"):
        # Many Arduino boards reset on serial open and need a short warm-up.
        sleep(4.0)
        return serial_obj, preferred_port, True

    try:
        serial_obj = SerialObject(portNo=None, digits=3)
    except Exception:
        serial_obj = None

    if hasattr(serial_obj, "ser"):
        detected_port = getattr(serial_obj.ser, "port", "AUTO")
        sleep(4.0)
        return serial_obj, detected_port, True

    return SerialObject.__new__(SerialObject), preferred_port, False


# Create a serial object to communicate with Arduino.
arduino, active_port, serial_ok = initialize_arduino("COM5")

# Initialize the last known positions of the servos
# Index 0: Left Hand, Index 1: Head, Index 2: Right Hand
last_positions = [180, 90, 0]

# Initialize pygame mixer for utility sound effects
pygame.mixer.init()

# Groq API configuration
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "your_groq_api_key_here")
groq_client = Groq(api_key=GROQ_API_KEY)

# Shared robot state for frontend visualization
state_lock = threading.Lock()
motion_lock = threading.Lock()
robot_state = {
    "status": "booting",
    "ai_provider": "groq",
    "model": "llama-3.3-70b-versatile",
    "port": active_port,
    "serial_connected": serial_ok,
    "voice_enabled": True,
    "servo_positions": [180, 90, 0],
    "last_heard": "",
    "last_response": "",
    "last_error": "",
    "updated_at": datetime.now().isoformat(timespec="seconds"),
}

ignore_mic_until = 0.0
last_serial_retry_ts = 0.0
SERIAL_RETRY_INTERVAL = 2.0
WAKE_WORDS = ("robot", "robo")
MOTION_KEYWORDS = (
    "look left",
    "look right",
    "left hand",
    "right hand",
    "say hi",
    "wave",
)


def update_state(**kwargs):
    with state_lock:
        robot_state.update(kwargs)
        robot_state["updated_at"] = datetime.now().isoformat(timespec="seconds")


def get_state_snapshot():
    with state_lock:
        return dict(robot_state)


def normalize_text(text):
    return " ".join((text or "").lower().split())


def is_motion_command(text):
    if any(keyword in text for keyword in MOTION_KEYWORDS):
        return True
    extra_motion_phrases = (
        "move head",
        "move your head",
        "turn head",
        "move hand",
        "move your hand",
        "move arm",
    )
    return any(keyword in text for keyword in extra_motion_phrases)


def classify_motion_intent(text):
    cmd = normalize_text(text)

    if "left" in cmd and "hand" in cmd:
        return "left_hand"
    if "say hi" in cmd or "wave" in cmd:
        return "left_hand"
    if "right" in cmd and "hand" in cmd:
        return "right_hand"
    if "look left" in cmd or ("head" in cmd and "left" in cmd):
        return "head_left"
    if "look right" in cmd or ("head" in cmd and "right" in cmd):
        return "head_right"
    if "look forward" in cmd or ("head" in cmd and "center" in cmd) or "straight" in cmd:
        return "head_center"
        
    return ""


SHORT_ALLOWED = ("hi", "hey")

def should_process_input(text):
    clean = normalize_text(text)
    if clean in SHORT_ALLOWED:
        return True
    if len(clean) < 3:
        return False
    return True


def strip_wake_word(text):
    clean = normalize_text(text)
    for word in WAKE_WORDS:
        if clean == word:
            return ""
        if clean.startswith(word + " "):
            return clean[len(word) + 1 :].strip()
    return clean


def looks_like_echo(text):
    clean = normalize_text(text)
    last = normalize_text(robot_state.get("last_response", ""))
    if len(clean) < 8 or len(last) < 8:
        return False
    if clean in last or last in clean:
        return True
    clean_words = set(clean.split())
    last_words = set(last.split())
    if not clean_words or not last_words:
        return False
    overlap = len(clean_words & last_words) / max(1, len(clean_words))
    return overlap >= 0.7


# ----------------- Frontend Dashboard -----------------#

app = Flask(__name__, template_folder="templates")


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    return jsonify(get_state_snapshot())


@app.route("/api/voice-mode", methods=["POST"])
def api_voice_mode():
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled", True)
    enabled = bool(enabled)
    # Voice mode controls microphone listening. Text/API commands remain available.
    update_state(voice_enabled=enabled, status="idle")
    return jsonify({"ok": True, "voice_enabled": enabled})


@app.route("/api/text", methods=["POST"])
def api_text():
    payload = request.get_json(silent=True) or {}
    text = normalize_text(payload.get("text", ""))
    if not text:
        return jsonify({"ok": False, "message": "Text is required"}), 400

    update_state(last_heard=text)
    response_text = process_user_text(text, from_voice=False, speak_response=False)
    return jsonify({"ok": True, "response": response_text, "state": get_state_snapshot()})


def start_dashboard():
    # Run frontend on a daemon thread so it does not block robot control loop.
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)

# Initialize Vosk model for offline speech recognition
# Prefer the original model path; fallback to the small model if that's what's installed.
base_dir = os.path.dirname(os.path.abspath(__file__))
model_candidates = [
    os.path.normpath(os.path.join(base_dir, "..", "Resources", "vosk-model-en-us-0.22")),
    os.path.normpath(os.path.join(base_dir, "..", "Resources", "vosk-model-small-en-us-0.15")),
]
model_path = next((path for path in model_candidates if os.path.isdir(path)), None)

if not model_path:
    raise FileNotFoundError(
        "No Vosk model found. Expected one of: " + ", ".join(model_candidates)
    )

model = vosk.Model(model_path)
recognizer = vosk.KaldiRecognizer(model, 16000)

# ----------------- Utility Functions -----------------#

def play_sound(file_path):
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        abs_path = os.path.normpath(os.path.join(base_dir, file_path))
        pygame.mixer.music.load(abs_path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(5)
    except:
        pass # Skip if file is missing

def text_to_speech(text, voice_index=1, rate=170, volume=1.0):
    """Local TTS using pyttsx3"""
    global ignore_mic_until
    if not robot_state.get("voice_enabled", True):
        print(f"Robot says (muted): {text}")
        update_state(status="idle", last_response=text)
        return

    engine = pyttsx3.init()
    voices = engine.getProperty('voices')
    
    if voice_index < len(voices):
        engine.setProperty('voice', voices[voice_index].id)
    
    engine.setProperty('rate', rate)
    engine.setProperty('volume', volume)
    
    print(f"Robot says: {text}")
    update_state(status="speaking", last_response=text)
    engine.say(text)
    engine.runAndWait()
    # Give the mic a short cooldown so the robot does not hear itself.
    ignore_mic_until = monotonic() + 2.7
    update_state(status="idle")

#----------------- Speech to Text Function -----------------#

def listen_with_vosk():
    global ignore_mic_until
    mic = pyaudio.PyAudio()
    stream = mic.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=8192)
    stream.start_stream()
    
    print("Listening...")
    update_state(status="listening")
    play_sound("../Resources/listening.mp3")

    while True:
        # Check if voice was disabled while we were listening
        if not robot_state.get("voice_enabled", True):
            stream.stop_stream()
            stream.close()
            mic.terminate()
            update_state(status="idle")
            return None

        data = stream.read(8192, exception_on_overflow=False)
        if monotonic() < ignore_mic_until:
            continue

        if len(data) == 0:
            continue

        if recognizer.AcceptWaveform(data):
            play_sound("../Resources/convert.mp3")
            result = recognizer.Result()
            text = json.loads(result)["text"]
            print(f"You said: {text}")
            update_state(status="processing", last_heard=text)
            stream.stop_stream()
            stream.close()
            mic.terminate()
            return text

# ----------------- AI Text Generation Function -----------------#

def groq_api(text):
    last_err = ""
    for _ in range(3):
        try:
            completion = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a friendly robot assistant. Give short, clear, spoken-style replies.",
                    },
                    {"role": "user", "content": text},
                ],
                temperature=0.7,
                max_tokens=200,
            )
            reply = completion.choices[0].message.content.strip()
            if reply:
                update_state(last_error="", status="idle")
                return reply
            last_err = "Empty AI response"
        except Exception as e:
            last_err = str(e)
            sleep(0.4)

    update_state(last_error=last_err, status="ai_error")
    return "I cannot reach Groq right now. Please check internet and API key, then try again."

# ----------------- Servo Control Function -----------------#

def _reconnect_arduino():
    """Wait for Arduino to reboot after brownout, then reconnect."""
    global arduino
    print("Arduino connection lost. Closing old connection and waiting for reboot...")
    # Close the old broken serial connection to release the COM port
    try:
        if hasattr(arduino, "ser") and arduino.ser is not None:
            arduino.ser.close()
    except Exception:
        pass
    # Clear the arduino object so hasattr checks fail cleanly
    arduino = SerialObject.__new__(SerialObject)
    sleep(4.0)  # Arduino takes ~2-3s to reboot after brownout
    arduino, detected_port, connected = initialize_arduino("COM5")
    update_state(port=detected_port, serial_connected=connected)
    if connected:
        print(f"Arduino reconnected on {detected_port}")
    else:
        print("Arduino reconnection failed.")
    return connected


def move_servo(target_position):
    global last_positions, arduino

    lock_acquired = motion_lock.acquire(timeout=5.0)
    if not lock_acquired:
        update_state(status="busy", last_error="Motion is busy. Please retry command.")
        return False

    try:
        bounded_target = [max(0, min(180, int(v))) for v in target_position]

        # --- Ensure Arduino is connected (with reconnection) ---
        if not hasattr(arduino, "ser"):
            connected = _reconnect_arduino()
            if not connected:
                update_state(
                    status="serial_error",
                    serial_connected=False,
                    last_error=f"Arduino not connected on {robot_state['port']}",
                )
                return False

        if bounded_target == last_positions:
            return True

        # Send a single command with the final target position
        myString = "$" + "".join([str(int(d)).zfill(3) for d in bounded_target])
        try:
            arduino.ser.write(myString.encode())
        except Exception as e:
            err_msg = str(e)
            print(f"Serial Exception: {err_msg}")
            # Arduino likely browned out — try to reconnect and retry once
            connected = _reconnect_arduino()
            if not connected:
                update_state(status="serial_error", serial_connected=False, last_error=f"Error: {err_msg}")
                return False
            try:
                arduino.ser.write(myString.encode())
            except Exception:
                update_state(status="serial_error", serial_connected=False, last_error="Retry failed after reconnect")
                return False

        # Update state
        last_positions = bounded_target[:]
        update_state(servo_positions=last_positions, serial_connected=True, status="idle", port=getattr(arduino.ser, "port", robot_state["port"]))
        return True
    finally:
        motion_lock.release()


def handle_motion_command(text):
    cmd = text.lower()
    motion_intent = classify_motion_intent(cmd)
    handled = False
    spoken = ""

    if motion_intent == "left_hand":
        handled = True
        ok = move_servo([0, last_positions[1], last_positions[2]])
        if ok:
            sleep(0.5)
            move_servo([180, last_positions[1], last_positions[2]])
        spoken = "Moving my left hand." if ok else "I cannot move my hand because Arduino is not connected."
    elif motion_intent == "right_hand":
        handled = True
        # Right hand is now index 2
        ok = move_servo([last_positions[0], last_positions[1], 180])
        if ok:
            sleep(0.5)
            move_servo([last_positions[0], last_positions[1], 0])
        spoken = "Moving my right hand." if ok else "I cannot move my hand because Arduino is not connected."
    elif motion_intent == "head_left":
        handled = True
        # Head is now index 1
        ok = move_servo([last_positions[0], 180, last_positions[2]])
        if ok:
            sleep(0.5)
            move_servo([last_positions[0], 90, last_positions[2]])
        spoken = "Looking left." if ok else "I cannot move my head because Arduino is not connected."
    elif motion_intent == "head_right":
        handled = True
        # Head is now index 1
        ok = move_servo([last_positions[0], 0, last_positions[2]])
        if ok:
            sleep(0.5)
            move_servo([last_positions[0], 90, last_positions[2]])
        spoken = "Looking right." if ok else "I cannot move my head because Arduino is not connected."
    elif motion_intent == "head_center":
        handled = True
        # Head is now index 1
        ok = move_servo([last_positions[0], 90, last_positions[2]])
        spoken = "Looking straight." if ok else "I cannot move my head because Arduino is not connected."

    return handled, spoken


def process_user_text(cleaned_input, from_voice=True, speak_response=True):
    if from_voice and looks_like_echo(cleaned_input):
        update_state(status="idle")
        return ""

    command_handled, command_reply = handle_motion_command(cleaned_input)
    if command_handled:
        if command_reply:
            if speak_response:
                text_to_speech(command_reply)
            else:
                update_state(last_response=command_reply, status="idle")
        return command_reply

    normalized_input = normalize_text(cleaned_input)
    if normalized_input in ("hello", "hi", "hey", "hey robot", "hi robot", "hello robot"):
        hello_gesture()
        greeting = "Hello! I am your robot assistant. How can I help you today?"
        if speak_response:
            text_to_speech(greeting, voice_index=0, rate=150)
        else:
            update_state(last_response=greeting, status="idle")
        return greeting

    print(f"Processing input: {cleaned_input}")
    response_text = groq_api(cleaned_input)
    if speak_response:
        text_to_speech(response_text)
    else:
        update_state(last_response=response_text, status="idle")
    return response_text

def hello_gesture():
    print("Executing Hello Gesture...")
    # Move left arm up (0 is up for Left Hand)
    move_servo([0, last_positions[1], last_positions[2]])
    sleep(0.4)
    for _ in range(2):
        # Wave back and forth with pauses so the servo can physically reach each position
        move_servo([90, last_positions[1], last_positions[2]])
        sleep(0.3)
        move_servo([0, last_positions[1], last_positions[2]])
        sleep(0.3)

    # Reset arm back to resting position
    sleep(0.4)
    move_servo([180, last_positions[1], last_positions[2]])

# ----------------- Main Loop -----------------#

# Start frontend dashboard for live hardware visualization
threading.Thread(target=start_dashboard, daemon=True).start()
print("Dashboard running at http://127.0.0.1:5000")
update_state(status="idle")

# Skipping startup gesture to avoid Arduino brownout from servo power draw.
# hello_gesture() will run when the user says "hello" or "say hi".

while True:
    if not robot_state.get("voice_enabled", True):
        update_state(status="idle")
        sleep(0.1)
        continue

    # 1. Listen for user voice
    user_input = listen_with_vosk()

    if not user_input:
        continue

    if not should_process_input(user_input):
        update_state(status="idle")
        continue

    cleaned_input = strip_wake_word(user_input)
    if not cleaned_input:
        text_to_speech("Yes? Please tell me a command or question.")
        continue

    process_user_text(cleaned_input, from_voice=True)