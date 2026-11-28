import os
import cv2
import mediapipe as mp
import math
import time
import sqlite3
import numpy as np
import re
from queue import Queue
from threading import Thread, Lock
from functools import wraps
from contextlib import contextmanager
import requests
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from twilio.rest import Client
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from eye_detection import EyeClosureTracker, eye_aspect_ratio

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.abspath(
    os.environ.get("DATABASE_PATH") or os.path.join(BASE_DIR, "drivesafe.db")
)

app = Flask(__name__)
secret_key = os.environ.get("SECRET_KEY")
if os.environ.get("RENDER", "").lower() == "true" and not secret_key:
    raise RuntimeError("SECRET_KEY must be configured in the Render environment.")
app.config["SECRET_KEY"] = secret_key or "local-development-only-change-before-deployment"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

# Monitoring state and detection parameters
monitoring = False
blink_count = 0
drowsiness_count = 0
last_ear = None
last_left_ear = None
last_right_ear = None
last_eye_state = "UNKNOWN"
last_closed_duration = 0.0
current_status = "Monitoring stopped"
face_lock = Lock()
sms_lock = Lock()
sms_queue = Queue()
sms_status_by_user = {}
last_sms_time_by_user = {}
last_sms_event_by_user = {}
incident_active = False

SMS_COOLDOWN = 60
SMS_MESSAGE = "DriveSafe Emergency Alert! Drowsiness detected. Are you sleeping? Please stay alert and stop driving safely."
TWILIO_SID_PATTERN = re.compile(r"^AC[0-9a-fA-F]{32}$")
E164_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
INDIA_MOBILE_PATTERN = re.compile(r"^\+91\d{10}$")

face_mesh = mp.solutions.face_mesh.FaceMesh(
    static_image_mode=False,
    max_num_faces=1,
    refine_landmarks=True,
    min_detection_confidence=0.3,
    min_tracking_confidence=0.3
)

LEFT_EYE = [362, 385, 387, 263, 373, 380]
RIGHT_EYE = [33, 160, 158, 133, 153, 144]


def read_float_setting(name, default):
    try:
        value = float(os.environ.get(name, default))
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number.") from error
    if not math.isfinite(value):
        raise RuntimeError(f"{name} must be a finite number.")
    return value


def read_int_setting(name, default):
    try:
        value = int(os.environ.get(name, default))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer.") from error
    return value


EYE_CLOSED_THRESHOLD = read_float_setting("EYE_CLOSED_THRESHOLD", "0.22")
EYE_OPEN_THRESHOLD = read_float_setting("EYE_OPEN_THRESHOLD", "0.26")
DROWSINESS_TIME = read_float_setting("DROWSINESS_TIME", "2.0")
BLINK_MIN_DURATION = read_float_setting("BLINK_MIN_DURATION", "0.08")
BLINK_MAX_DURATION = read_float_setting("BLINK_MAX_DURATION", "0.80")
EYE_CLOSED_CONFIRM_FRAMES = read_int_setting("EYE_CLOSED_CONFIRM_FRAMES", "2")
if not 0 < EYE_CLOSED_THRESHOLD < EYE_OPEN_THRESHOLD < 1:
    raise RuntimeError("Eye thresholds must satisfy 0 < closed threshold < open threshold < 1.")
if not 0 < BLINK_MIN_DURATION <= BLINK_MAX_DURATION < DROWSINESS_TIME:
    raise RuntimeError("Blink durations must be positive, ordered, and below DROWSINESS_TIME.")
if DROWSINESS_TIME <= 0 or EYE_CLOSED_CONFIRM_FRAMES < 1:
    raise RuntimeError("DROWSINESS_TIME and EYE_CLOSED_CONFIRM_FRAMES must be positive.")
eye_tracker = EyeClosureTracker(
    close_threshold=EYE_CLOSED_THRESHOLD,
    open_threshold=EYE_OPEN_THRESHOLD,
    drowsiness_seconds=DROWSINESS_TIME,
    blink_min_seconds=BLINK_MIN_DURATION,
    blink_max_seconds=BLINK_MAX_DURATION,
    confirmation_frames=EYE_CLOSED_CONFIRM_FRAMES,
)


@contextmanager
def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db():
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    with get_db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                phone_number TEXT NOT NULL DEFAULT '',
                sms_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        if "phone_number" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN phone_number TEXT NOT NULL DEFAULT ''")
        if "sms_enabled" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN sms_enabled INTEGER NOT NULL DEFAULT 0")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_alert_history_user_id_id "
            "ON alert_history (user_id, id DESC)"
        )


def twilio_configuration_errors():
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    phone = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
    errors = []
    if not sid:
        errors.append("TWILIO_ACCOUNT_SID is missing.")
    elif not TWILIO_SID_PATTERN.fullmatch(sid):
        errors.append("TWILIO_ACCOUNT_SID is not a valid Twilio Account SID.")
    if not token:
        errors.append("TWILIO_AUTH_TOKEN is missing.")
    elif len(token) != 32 or token.lower() in ("your_twilio_auth_token_here",):
        errors.append("TWILIO_AUTH_TOKEN is not a valid 32-character Twilio Auth Token.")
    if not phone:
        errors.append("TWILIO_PHONE_NUMBER is missing.")
    elif not E164_PATTERN.fullmatch(phone):
        errors.append("TWILIO_PHONE_NUMBER must be in E.164 format, such as +15551234567.")
    return errors


def twilio_ready():
    return not twilio_configuration_errors()


def sms_error_message(error):
    code = getattr(error, "code", None)
    if code == 20003:
        return "Twilio authentication failed. Check the Account SID and Auth Token in the server environment settings."
    if code in (21211, 21212, 21213, 21614):
        return "Twilio rejected the phone number. Check the sender and recipient numbers."
    if code == 21608:
        return "Twilio trial accounts can only message verified recipient numbers."
    if code == 21408:
        return "Messaging to this country is not enabled in your Twilio account."
    if code == 21606:
        return "The Twilio sender number is not valid for SMS. Check your Twilio number and messaging service."

    error_text = str(getattr(error, "msg", "") or "").lower()
    if any(term in error_text for term in ("balance", "insufficient funds", "billing", "credit")):
        return "Twilio account has insufficient balance or a billing issue. Check Billing in the Twilio Console."
    if isinstance(error, requests.exceptions.RequestException):
        return "Could not connect to Twilio. Check the network connection and try again."
    status_code = getattr(error, "status", None)
    if status_code:
        return f"Twilio rejected the SMS request (HTTP {status_code}, error {code or 'unknown'}). Check the Twilio Console."
    return "Twilio could not process the SMS request. Check the Twilio Console and server connection."


def set_sms_failure(user_id, recipient, error_message):
    now = int(time.time() * 1000)
    with sms_lock:
        sms_status_by_user[user_id] = "SMS FAILED"
        last_sms_event_by_user[user_id] = {
            "type": "failed",
            "status": "SMS FAILED",
            "recipient": recipient,
            "timestamp": now,
            "error": error_message
        }


def sms_worker():
    while True:
        user_id, recipient, is_test = sms_queue.get()
        try:
            client = Client(
                os.environ["TWILIO_ACCOUNT_SID"].strip(),
                os.environ["TWILIO_AUTH_TOKEN"].strip(),
                http_client=TwilioHttpClient(timeout=10)
            )
            msg_body = SMS_MESSAGE if not is_test else f"[TEST] {SMS_MESSAGE}"
            message = client.messages.create(body=msg_body, from_=os.environ["TWILIO_PHONE_NUMBER"].strip(), to=recipient)
            now = time.time()
            message_sid = getattr(message, "sid", None)
            if not message_sid:
                set_sms_failure(user_id, recipient, "Twilio returned no message ID; the request was not confirmed.")
                continue
            with sms_lock:
                sms_status_by_user[user_id] = "SMS SENT"
                last_sms_time_by_user[user_id] = now
                last_sms_event_by_user[user_id] = {
                    "type": "sent",
                    "status": "SMS SENT",
                    "recipient": recipient,
                    "timestamp": int(now * 1000),
                    "sid": message_sid
                }
            app.logger.info("Twilio accepted an SMS request.")
        except TwilioRestException as error:
            app.logger.warning("Twilio rejected an SMS request (HTTP %s, error %s).", error.status, error.code)
            set_sms_failure(user_id, recipient, sms_error_message(error))
        except (TwilioException, requests.exceptions.RequestException) as error:
            app.logger.warning("Twilio SMS request failed: %s.", type(error).__name__)
            set_sms_failure(user_id, recipient, sms_error_message(error))
        finally:
            sms_queue.task_done()


Thread(target=sms_worker, daemon=True).start()


def queue_alert_sms(user_id, is_test=False):
    if not user_id:
        return False, "User session not found"
    with get_db() as conn:
        user = conn.execute("SELECT phone_number, sms_enabled FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or not user["sms_enabled"]:
        with sms_lock:
            sms_status_by_user[user_id] = "SMS WAITING"
        return False, "SMS alerts disabled in settings"
    if not twilio_ready():
        error_message = " ".join(twilio_configuration_errors())
        set_sms_failure(user_id, user["phone_number"], error_message)
        return False, error_message
    if not INDIA_MOBILE_PATTERN.fullmatch(user["phone_number"] or ""):
        error_message = "Save a valid Indian mobile number in +91 followed by 10 digits format."
        set_sms_failure(user_id, user["phone_number"], error_message)
        return False, error_message

    now = time.time()
    with sms_lock:
        last_time = last_sms_time_by_user.get(user_id, 0)
        if (now - last_time) < SMS_COOLDOWN:
            remaining = int(SMS_COOLDOWN - (now - last_time))
            last_sms_event_by_user[user_id] = {
                "type": "cooldown",
                "status": sms_status_by_user.get(user_id, "SMS WAITING"),
                "recipient": user["phone_number"],
                "timestamp": int(time.time() * 1000),
                "remaining": remaining
            }
            return False, f"SMS alert in cooldown ({remaining}s remaining)"

        if sms_status_by_user.get(user_id) == "SMS SENDING":
            return False, "SMS already queued for dispatch"

        sms_status_by_user[user_id] = "SMS SENDING"
        last_sms_event_by_user[user_id] = {
            "type": "sending",
            "status": "SMS SENDING",
            "recipient": user["phone_number"],
            "timestamp": int(time.time() * 1000)
        }

    sms_queue.put((user_id, user["phone_number"], is_test))
    return True, "SMS SENDING"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            api_paths = ("/process_frame", "/status", "/start", "/stop", "/alerts")
            if request.path in api_paths or request.path.startswith("/sms/"):
                return jsonify({"error": "Please log in first."}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def eye_ratio(points, indexes):
    return eye_aspect_ratio(points, indexes)


def get_driver_state(detection_text, is_monitoring=None):
    if is_monitoring is None:
        is_monitoring = monitoring
    if not is_monitoring:
        return "STOPPED"
    if detection_text == "DROWSINESS ALERT!":
        return "DROWSY"
    if detection_text in ("Eyes Closing", "Eyes Closed"):
        return "WARNING"
    if detection_text == "Face not detected":
        return "NO FACE"
    if detection_text == "Eyes Open":
        return "ALERT"
    return "UNKNOWN"


def detection_telemetry():
    return {
        "eye_ratio": last_ear,
        "left_eye_ratio": last_left_ear,
        "right_eye_ratio": last_right_ear,
        "eye_state": last_eye_state,
        "closed_duration": last_closed_duration,
        "ear_threshold": EYE_CLOSED_THRESHOLD,
        "eye_closed_threshold": EYE_CLOSED_THRESHOLD,
        "eye_open_threshold": EYE_OPEN_THRESHOLD,
        "drowsiness_time": DROWSINESS_TIME,
        "blink_min_duration": BLINK_MIN_DURATION,
        "blink_max_duration": BLINK_MAX_DURATION,
    }


def reset_eye_telemetry():
    global last_ear, last_left_ear, last_right_ear, last_eye_state, last_closed_duration
    eye_tracker.reset()
    last_ear = None
    last_left_ear = None
    last_right_ear = None
    last_eye_state = "UNKNOWN"
    last_closed_duration = 0.0


@app.route("/")
@login_required
def home():
    with get_db() as conn:
        user = conn.execute("SELECT phone_number, sms_enabled FROM users WHERE id=?", (session["user_id"],)).fetchone()
    phone_val = user["phone_number"] if user and user["phone_number"] else ""
    sms_on = bool(user["sms_enabled"]) if user else False
    return render_template(
        "index.html",
        user_name=session.get("full_name", "Driver"),
        phone_number=phone_val,
        sms_enabled=sms_on,
        twilio_configured=twilio_ready(),
        sms_cooldown=SMS_COOLDOWN
    )


@app.get("/healthz")
def health_check():
    try:
        with get_db() as connection:
            connection.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        app.logger.exception("Database health check failed.")
        return jsonify({"status": "error", "error": "Database unavailable"}), 503
    return jsonify({"status": "ok"})


@app.route("/alerts", methods=["GET", "POST", "DELETE"])
@login_required
def alert_history():
    user_id = session["user_id"]
    if request.method == "GET":
        with get_db() as connection:
            rows = connection.execute(
                "SELECT id, event_type, message, created_at_ms "
                "FROM alert_history WHERE user_id=? ORDER BY id DESC LIMIT 20",
                (user_id,),
            ).fetchall()
        return jsonify({
            "events": [
                {
                    "id": row["id"],
                    "type": row["event_type"],
                    "message": row["message"],
                    "timestamp": row["created_at_ms"],
                }
                for row in reversed(rows)
            ]
        })

    if request.method == "DELETE":
        with get_db() as connection:
            connection.execute("DELETE FROM alert_history WHERE user_id=?", (user_id,))
        return jsonify({"status": "cleared"})

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON alert event is required."}), 400
    event_type = data.get("type")
    message = data.get("message")
    if event_type not in ("critical", "warning", "sms", "system"):
        return jsonify({"error": "Invalid alert event type."}), 400
    if not isinstance(message, str) or not message.strip() or len(message) > 500:
        return jsonify({"error": "Alert message must contain 1 to 500 characters."}), 400

    timestamp = int(time.time() * 1000)
    with get_db() as connection:
        cursor = connection.execute(
            "INSERT INTO alert_history (user_id, event_type, message, created_at_ms) "
            "VALUES (?, ?, ?, ?)",
            (user_id, event_type, message.strip(), timestamp),
        )
    return jsonify({
        "id": cursor.lastrowid,
        "type": event_type,
        "message": message.strip(),
        "timestamp": timestamp,
    }), 201


@app.route("/sms/settings", methods=["POST"])
@login_required
def sms_settings():
    phone = request.form.get("phone_number", "").strip()
    enabled = request.form.get("sms_enabled") == "on"
    if phone and not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        return jsonify({"error": "Enter a valid phone number in international E.164 format, e.g. +14155552671."}), 400
    with get_db() as conn:
        conn.execute("UPDATE users SET phone_number=?, sms_enabled=? WHERE id=?", (phone, int(enabled), session["user_id"]))
    with sms_lock:
        sms_status_by_user[session["user_id"]] = "SMS WAITING"
    status_text = "SMS WAITING"
    return jsonify({"status": status_text, "phone_number": phone, "sms_enabled": enabled})


@app.route("/sms/test", methods=["POST"])
@login_required
def sms_test():
    with get_db() as conn:
        user = conn.execute("SELECT phone_number, sms_enabled FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if not user or not user["phone_number"]:
        return jsonify({"error": "Please enter and save your mobile phone number in international format first."}), 400
    if not user["sms_enabled"]:
        return jsonify({"error": "Please enable SMS alerts checkbox and save settings."}), 400
    if not twilio_ready():
        error_message = " ".join(twilio_configuration_errors())
        set_sms_failure(session["user_id"], user["phone_number"], error_message)
        return jsonify({"error": error_message, "status": "SMS FAILED"}), 400

    ok, msg = queue_alert_sms(session["user_id"], is_test=True)
    if ok:
        return jsonify({"status": "SMS SENDING", "message": "Test SMS request is being sent to Twilio."}), 202
    status_code = 429 if "cooldown" in msg.lower() or "already queued" in msg.lower() else 400
    with sms_lock:
        state = sms_status_by_user.get(session["user_id"], "SMS WAITING")
    if state not in ("SMS WAITING", "SMS SENDING", "SMS SENT", "SMS FAILED"):
        state = "SMS WAITING"
    return jsonify({"error": msg, "status": state}), status_code


@app.route("/sms/status")
@login_required
def sms_status():
    uid = session["user_id"]
    with get_db() as conn:
        user = conn.execute("SELECT phone_number, sms_enabled FROM users WHERE id=?", (uid,)).fetchone()
    with sms_lock:
        state = sms_status_by_user.get(uid)
        last_event = last_sms_event_by_user.get(uid)
        last_time = last_sms_time_by_user.get(uid, 0)

    now = time.time()
    cooldown_rem = max(0, int(SMS_COOLDOWN - (now - last_time))) if (now - last_time) < SMS_COOLDOWN else 0

    display_status = state if state in ("SMS WAITING", "SMS SENDING", "SMS SENT", "SMS FAILED") else "SMS WAITING"
    configuration_errors = twilio_configuration_errors()

    return jsonify({
        "status": display_status,
        "detail": state or "SMS WAITING",
        "phone_number": user["phone_number"] if user else "",
        "sms_enabled": bool(user["sms_enabled"]) if user else False,
        "twilio_configured": not configuration_errors,
        "configuration_error": " ".join(configuration_errors),
        "recipient_configured": bool(user and INDIA_MOBILE_PATTERN.fullmatch(user["phone_number"] or "")),
        "cooldown_remaining": cooldown_rem,
        "last_event": last_event
    })


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("home"))
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not full_name or not email or not password or not confirm_password:
            flash("Please fill in all fields.", "error")
        elif len(full_name) > 80:
            flash("Name must be 80 characters or fewer.", "error")
        elif len(password) < 8:
            flash("Password must contain at least 8 characters.", "error")
        elif password != confirm_password:
            flash("Passwords do not match.", "error")
        else:
            try:
                with get_db() as connection:
                    cursor = connection.execute(
                        "INSERT INTO users (full_name, email, password_hash) VALUES (?, ?, ?)",
                        (full_name, email, generate_password_hash(password))
                    )
                    user_id = cursor.lastrowid
                session.clear()
                session["user_id"] = user_id
                session["full_name"] = full_name
                session["email"] = email
                flash("Your account has been created. Welcome to DriveSafe!", "success")
                return redirect(url_for("home"))
            except sqlite3.IntegrityError:
                flash("An account with this email already exists. Please log in.", "error")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("home"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with get_db() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["full_name"] = user["full_name"]
            session["email"] = user["email"]
            return redirect(url_for("home"))
        flash("Invalid email or password. Please try again.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    global monitoring, current_status, incident_active
    monitoring = False
    incident_active = False
    reset_eye_telemetry()
    current_status = "Monitoring stopped"
    session.clear()
    return redirect(url_for("login"))


@app.route("/start")
@login_required
def start():
    global monitoring, blink_count, drowsiness_count, current_status, incident_active
    session["monitoring"] = True
    blink_count = 0
    drowsiness_count = 0
    incident_active = False
    reset_eye_telemetry()
    current_status = "Face not detected"
    monitoring = True
    return jsonify({
        "status": "Monitoring started",
        "monitoring": True,
        "blink_count": 0,
        "drowsiness_count": 0,
        "driver_state": "NO FACE",
        **detection_telemetry(),
    })


@app.route("/stop")
@login_required
def stop():
    global monitoring, current_status, incident_active
    session["monitoring"] = False
    monitoring = False
    incident_active = False
    reset_eye_telemetry()
    current_status = "Monitoring stopped"
    return jsonify({
        "status": "Monitoring stopped",
        "monitoring": False,
        "driver_state": "STOPPED",
        **detection_telemetry(),
    })


@app.route("/process_frame", methods=["POST"])
@login_required
def process_frame():
    global monitoring, blink_count, drowsiness_count, current_status, incident_active
    global last_ear, last_left_ear, last_right_ear, last_eye_state, last_closed_duration
    uid = session.get("user_id")

    if not session.get("monitoring", False):
        return jsonify({
            "monitoring": False,
            "blink_count": blink_count,
            "drowsiness_count": drowsiness_count,
            "detection_status": "Monitoring stopped",
            "driver_state": "STOPPED",
            **detection_telemetry(),
        })

    monitoring = True
    file = request.files.get("frame")
    if file is None:
        return jsonify({"error": "No camera frame received"}), 400

    try:
        image_bytes = file.read()
        image_array = np.frombuffer(image_bytes, np.uint8)
        frame = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        if frame is None:
            return jsonify({"error": "Invalid camera frame"}), 400

        height, width = frame.shape[:2]
        if width > 640:
            new_height = int(height * 640 / width)
            frame = cv2.resize(frame, (640, new_height))

        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb_frame.flags.writeable = False

        with face_lock:
            results = face_mesh.process(rgb_frame)

        if not results.multi_face_landmarks:
            current_status = "Face not detected"
            incident_active = False
            reset_eye_telemetry()
            with sms_lock:
                sms_st = sms_status_by_user.get(uid, "SMS WAITING")
                sms_ev = last_sms_event_by_user.get(uid)
            return jsonify({
                "monitoring": True,
                "blink_count": blink_count,
                "drowsiness_count": drowsiness_count,
                "detection_status": current_status,
                "driver_state": "NO FACE",
                **detection_telemetry(),
                "sms_status": sms_st,
                "sms_event": sms_ev
            })

        face = results.multi_face_landmarks[0]
        h, w = frame.shape[:2]
        points = [(landmark.x * w, landmark.y * h) for landmark in face.landmark]
        left_ear = eye_ratio(points, LEFT_EYE)
        right_ear = eye_ratio(points, RIGHT_EYE)
        average_ear = (left_ear + right_ear) / 2
        last_ear = round(average_ear, 3)
        last_left_ear = round(left_ear, 3)
        last_right_ear = round(right_ear, 3)
        observation = eye_tracker.update(left_ear, right_ear, time.monotonic())
        last_eye_state = observation.state
        last_closed_duration = round(observation.closed_duration, 2)

        if observation.state == "CLOSED":
            if observation.drowsy:
                current_status = "DROWSINESS ALERT!"
                if not incident_active:
                    incident_active = True
                    drowsiness_count += 1
                    queue_alert_sms(uid)
            else:
                current_status = "Eyes Closed"
        elif observation.state == "CLOSING":
            current_status = "Eyes Closing"
        elif observation.state == "OPEN":
            if observation.blink_detected:
                blink_count += 1
            incident_active = False
            current_status = "Eyes Open"
        else:
            current_status = "Face not detected"
            incident_active = False

        driver_state = get_driver_state(current_status)

        with sms_lock:
            sms_raw = sms_status_by_user.get(uid, "SMS WAITING")
            sms_st = sms_raw if sms_raw in ("SMS WAITING", "SMS SENDING", "SMS SENT", "SMS FAILED") else "SMS WAITING"
            sms_ev = last_sms_event_by_user.get(uid)

        return jsonify({
            "monitoring": True,
            "blink_count": blink_count,
            "drowsiness_count": drowsiness_count,
            "detection_status": current_status,
            "driver_state": driver_state,
            **detection_telemetry(),
            "sms_status": sms_st,
            "sms_event": sms_ev
        })

    except Exception:
        app.logger.exception("Frame processing failed.")
        return jsonify({"error": "Frame processing failed. Please try again."}), 500


@app.route("/status")
@login_required
def status():
    uid = session.get("user_id")
    is_monitoring = session.get("monitoring", False)
    with sms_lock:
        current_sms = sms_status_by_user.get(uid)
        sms_ev = last_sms_event_by_user.get(uid)

    sms_st = current_sms if current_sms in ("SMS WAITING", "SMS SENDING", "SMS SENT", "SMS FAILED") else "SMS WAITING"

    return jsonify({
        "monitoring": is_monitoring,
        "blink_count": blink_count,
        "drowsiness_count": drowsiness_count,
        "detection_status": current_status,
        "driver_state": get_driver_state(current_status, is_monitoring),
        **detection_telemetry(),
        "sms_status": sms_st,
        "sms_event": sms_ev
    })


init_db()

if __name__ == "__main__":
    port_num = int(os.environ.get("PORT", 5000))
    print(f"=================================================")
    print(f"  DriveSafe Driver Drowsiness Monitoring System  ")
    print(f"  Running locally on http://127.0.0.1:{port_num} ")
    print(f"  Twilio SMS Configured: {twilio_ready()}        ")
    for configuration_error in twilio_configuration_errors():
        print(f"  SMS setup required: {configuration_error}")
    print(f"=================================================")
    app.run(host="0.0.0.0", port=port_num, debug=False, use_reloader=False)
