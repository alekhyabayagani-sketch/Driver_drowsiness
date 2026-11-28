# DriveSafe | Real-Time Driver Drowsiness Detection System

A modern, real-time driver fatigue monitoring system utilizing OpenCV and MediaPipe Face Mesh landmarks to analyze Eye Aspect Ratio (EAR), detect eye blinks, identify drowsiness events, sound audio alarms, and dispatch emergency mobile SMS notifications via Twilio.

---

## Key Features

- **Live Vision Stream & Eye Aspect Ratio (EAR)**: Real-time tracking of facial landmarks using MediaPipe Face Mesh at low latency.
- **Eye Blink & Fatigue Analytics**: Hysteresis-based EAR detection counts confirmed blinks and alerts on prolonged eye closure.
- **Dual Audio Alarm System**: In-browser Web Audio API square-wave buzzer + Web Speech API voice alarm ("Are you sleeping? Please stay alert!").
- **Twilio Mobile SMS Dispatch**: Asynchronous daemon queue sending SMS alerts directly to the driver's or supervisor's phone upon confirmed fatigue events.
- **SMS Rate Limiting & Cooldown**: Enforces a 60-second cooldown period between SMS notifications to prevent spamming while eyes remain closed.
- **Commercial Telematics Dashboard**: Responsive dark navy UI featuring live EAR waveforms, alertness gauges, session duration timer, and real-time alert history logs.
- **Enterprise Security**: User authentication using Werkzeug password hashing, SQLite session management, and server-side `.env` credential isolation.

---

## Quick Start on Windows

### 1. Prerequisites
- Python 3.10+ or Python 3.11 installed on your computer.
- A functional webcam.

### 2. Open PowerShell in Project Folder
Navigate to your project directory:
```powershell
cd "c:\Users\alekh\OneDrive\Documents\driver_drowsiness"
```

### 3. Create and Activate Virtual Environment (Optional but Recommended)
```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 4. Install Dependencies
```powershell
python -m pip install -r requirements.txt
```

### 5. Configure Twilio SMS Credentials
Copy `.env.example` to `.env` if you haven't already:
```powershell
Copy-Item .env.example .env
```

Open `.env` in any text editor and fill in your Twilio credentials from the [Twilio Console](https://console.twilio.com/):
```env
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_32_character_auth_token
TWILIO_PHONE_NUMBER=+15551234567
```
The SID and Auth Token must be real credentials from your Twilio Console; sample values are rejected. The sender must be an SMS-capable Twilio number in E.164 format. The app does not simulate SMS: if configuration is missing or malformed, the dashboard displays the specific setup error and no SMS is reported as sent.

### Optional eye-detection tuning
Detection defaults are listed in `.env.example` and can be overridden in local `.env` or Render environment settings:

| Variable | Default | Purpose |
|---|---:|---|
| `EYE_CLOSED_THRESHOLD` | `0.22` | Both eye EAR values must fall below this to begin closure |
| `EYE_OPEN_THRESHOLD` | `0.26` | Average EAR must reach this to confirm reopening |
| `EYE_CLOSED_CONFIRM_FRAMES` | `2` | Consecutive closed frames required to reject one-frame noise |
| `BLINK_MIN_DURATION` / `BLINK_MAX_DURATION` | `0.08` / `0.80` | Accepted blink duration range, in seconds |
| `DROWSINESS_TIME` | `2.0` | Continuous confirmed closure duration before alert |

The open threshold must be higher than the closed threshold. The dashboard displays both eye EAR values, eye state, blink count, and closure duration while monitoring.

### 6. Run the Flask Application
```powershell
python app.py
```
Or with py launcher:
```powershell
py app.py
```

### 7. Access Dashboard
1. Open your browser and navigate to:
   ```
   http://127.0.0.1:5000
   ```
2. Log in with your existing account, or click **Create account** to register a new user.
3. In the right-hand panel under **Mobile SMS Dispatch**:
   - Enter your Indian mobile number as `+91` followed by 10 digits (e.g., `+919876543210`).
   - Check **Enable Mobile SMS Fatigue Alerts**.
   - Click **Save Settings**.
   - Click **Send Test SMS** to check that Twilio accepts a test request. The dashboard reports request acceptance; carrier delivery can take longer and depends on your Twilio account and mobile carrier.
4. Click **▶ Start Monitoring** to begin live camera surveillance.
5. Click **🔊 Sound Off** to toggle on the audible buzzer and voice synthesizer alarm.

---

## Deploy on Render

The repository includes a Render Blueprint, a Gunicorn `Procfile`, and a Python 3.11 runtime selection. The Blueprint is set to Render's free web-service plan and stores SQLite at `/tmp/drivesafe.db`. This is a best-effort public demo: the database is ephemeral, so accounts and saved SMS settings can be lost when the service restarts or redeploys. The free instance has limited memory, and MediaPipe may exceed it. Do not use this configuration for important accounts or data. Persistent storage requires a paid Render service and disk.

### English: deployment steps

1. Push the project to your GitHub repository. Do not add `.env` or `drivesafe.db`; `.env` is ignored, and the database file is kept out of version control. Check the GitHub repository and its history if the database was pushed in an earlier commit.
2. Sign in at [Render](https://dashboard.render.com), choose **New → Blueprint**, connect your GitHub account, and select this repository and branch. Render reads `render.yaml` from the repository root.
3. Review the Blueprint, confirm it shows the free plan and no disk, then apply it. The service builds with `pip install -r requirements.txt` and starts with Gunicorn. If Render asks for a payment method or card authorization, stop; do not enter card details.
4. In the service's **Environment** settings, add the Twilio values:
   - `TWILIO_ACCOUNT_SID` — your Twilio Account SID
   - `TWILIO_AUTH_TOKEN` — your Twilio Auth Token
   - `TWILIO_PHONE_NUMBER` — your SMS-capable Twilio sender number in E.164 format

   These values are intentionally not stored in Git. Render generates `SECRET_KEY`; leave `DATABASE_PATH` and `COOKIE_SECURE` as configured by the Blueprint.
5. Wait for the deploy to finish successfully. In Render, open the service and click its `onrender.com` URL. The public URL is shown at the top of the service page.
6. Create an account on the deployed site. Local accounts in your computer's SQLite file are not copied to Render.
7. Allow camera access when your browser asks, then click **Start Monitoring**. Render serves the site over HTTPS; the browser camera API requires HTTPS (or localhost during local development).
8. For SMS, save a recipient number, enable SMS, then click **Send Test SMS**. Twilio trial accounts may only send to verified recipient numbers. A successful response means Twilio accepted the request; it does not guarantee carrier delivery.

### తెలుగు: Render లో deploy చేసే విధానం

1. ప్రాజెక్ట్‌ను GitHub repository కి push చేయండి. `.env` లేదా `drivesafe.db` ఫైల్‌ను push చేయవద్దు. `.env` ignore అవుతుంది; database ఫైల్ కూడా version control లో ఉండకూడదు. Database గతంలో push చేసి ఉంటే, GitHub repository history ని కూడా పరిశీలించండి.
2. [Render](https://dashboard.render.com) లో sign in చేసి **New → Blueprint** ఎంచుకోండి. GitHub connect చేసి, ఈ repository మరియు branch ఎంచుకోండి. Render, root లోని `render.yaml` ఫైల్‌ను చదువుతుంది.
3. Blueprint లో free plan మరియు disk లేదని పరిశీలించి, ఆ తర్వాత apply చేయండి. Build కోసం `pip install -r requirements.txt`, server కోసం Gunicorn వాడబడతాయి. Render payment method లేదా card authorization అడిగితే ఆపండి; card వివరాలు నమోదు చేయవద్దు.
4. Service లోని **Environment** settings లో ఈ Twilio విలువలను మీరే జోడించండి:
   - `TWILIO_ACCOUNT_SID` — Twilio Account SID
   - `TWILIO_AUTH_TOKEN` — Twilio Auth Token
   - `TWILIO_PHONE_NUMBER` — SMS పంపగల Twilio sender number (`+` తో మొదలయ్యే E.164 format)

   ఈ secrets GitHub లో ఉండవు. `SECRET_KEY` Render ద్వారా తయారవుతుంది; Blueprint లోని `DATABASE_PATH`, `COOKIE_SECURE` విలువలను మార్చవద్దు.
5. Deploy విజయవంతమయ్యే వరకు వేచి ఉండండి. Render service పేజీలో కనిపించే `onrender.com` URL మీ public URL.
6. ఆ website లో కొత్త account సృష్టించండి. మీ కంప్యూటర్‌లోని SQLite accounts Render కి కాపీ కావు.
7. Browser అడిగినప్పుడు camera permission ఇవ్వండి; తర్వాత **Start Monitoring** నొక్కండి. Render HTTPS ఇస్తుంది. Browser camera కోసం HTTPS అవసరం.
8. SMS కోసం recipient number save చేసి, SMS enable చేసి, **Send Test SMS** నొక్కండి. Twilio trial account లో ముందుగా verify చేసిన నంబర్లకే SMS వెళ్లవచ్చు. Twilio request స్వీకరించిందని మాత్రమే success అర్థం; carrier delivery కి హామీ కాదు.

### Runtime and storage notes

- The browser captures webcam frames and sends them to Flask for MediaPipe/OpenCV processing. Render does not need a server-attached webcam. The dashboard, blink/EAR detection, timer, alert history, red drowsiness banner, voice alarm, and SMS controls remain in the existing application.
- Python 3.11 is selected for MediaPipe wheel compatibility. The project installs one pinned OpenCV contrib distribution, which MediaPipe depends on, avoiding overlapping OpenCV wheels that provide the same `cv2` module. NumPy is constrained to a compatible version, and the app uses image-processing APIs without requiring a desktop display.
- SQLite stores user accounts, password hashes, and SMS settings. This free Blueprint stores its database at `/tmp/drivesafe.db` without a persistent disk, so those records can disappear after restarts or redeploys. Free services can also sleep after inactivity. MediaPipe/OpenCV may exceed the free instance's 512 MB memory; a successful Render build and health check are not guaranteed.
- Monitoring counters and the SMS queue are currently in process memory; they reset when the service restarts. Alert incident history is stored in SQLite per user. The app uses one Gunicorn worker to keep its existing shared detection state consistent.
- If camera access fails, confirm that you opened the `https://` URL, allowed camera permissions, and are using a browser with webcam support. If SMS fails, check the service logs and Twilio settings in Render; never paste credentials into logs or GitHub.
