import importlib
import os
import tempfile
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

_test_database = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_test_database.name, "test.db")

from eye_detection import EyeClosureTracker, eye_aspect_ratio

drivesafe = importlib.import_module("app")


class TestEyeAspectRatio(unittest.TestCase):
    def test_open_eye_ratio(self):
        points = [
            (0.0, 0.5),
            (0.2, 0.485),
            (0.8, 0.485),
            (1.0, 0.5),
            (0.8, 0.515),
            (0.2, 0.515),
        ]
        self.assertAlmostEqual(eye_aspect_ratio(points, range(6)), 0.03)

    def test_rejects_degenerate_eye_landmarks(self):
        points = [(0.5, 0.5)] * 6
        with self.assertRaises(ValueError):
            eye_aspect_ratio(points, range(6))


class TestEyeClosureTracker(unittest.TestCase):
    def make_tracker(self):
        return EyeClosureTracker(0.22, 0.26, 2.0, 0.08, 0.8, 2)

    def test_counts_a_confirmed_blink_on_reopen(self):
        tracker = self.make_tracker()
        self.assertEqual(tracker.update(0.12, 0.13, 0.0).state, "CLOSING")
        self.assertEqual(tracker.update(0.12, 0.13, 0.1).state, "CLOSED")

        reopened = tracker.update(0.30, 0.29, 0.2)

        self.assertEqual(reopened.state, "OPEN")
        self.assertTrue(reopened.blink_detected)
        self.assertFalse(reopened.drowsy)

    def test_ignores_single_frame_noise_and_one_closed_eye(self):
        tracker = self.make_tracker()
        self.assertEqual(tracker.update(0.12, 0.13, 0.0).state, "CLOSING")
        self.assertFalse(tracker.update(0.30, 0.29, 0.1).blink_detected)
        self.assertEqual(tracker.update(0.12, 0.30, 0.2).state, "OPEN")

    def test_reports_drowsiness_after_configured_continuous_closure(self):
        tracker = self.make_tracker()
        tracker.update(0.12, 0.13, 0.0)
        tracker.update(0.12, 0.13, 0.1)

        observation = tracker.update(0.12, 0.13, 2.1)

        self.assertEqual(observation.state, "CLOSED")
        self.assertTrue(observation.drowsy)
        reopened = tracker.update(0.30, 0.29, 2.2)
        self.assertFalse(reopened.blink_detected)

    def test_hysteresis_keeps_eyes_closed_until_open_threshold(self):
        tracker = self.make_tracker()
        tracker.update(0.12, 0.13, 0.0)
        tracker.update(0.12, 0.13, 0.1)

        self.assertEqual(tracker.update(0.24, 0.24, 0.2).state, "CLOSED")
        self.assertEqual(tracker.update(0.27, 0.27, 0.3).state, "OPEN")


class SequencedTracker(EyeClosureTracker):
    def __init__(self, times):
        super().__init__(0.22, 0.26, 2.0, 0.08, 0.8, 2)
        self.times = iter(times)

    def update(self, left_ear, right_ear, now):
        return super().update(left_ear, right_ear, next(self.times))


def make_face_landmarks(left_ear, right_ear):
    landmarks = [SimpleNamespace(x=0.5, y=0.5) for _ in range(478)]
    for indexes, base_x, ear in (
        (drivesafe.LEFT_EYE, 0.1, left_ear),
        (drivesafe.RIGHT_EYE, 0.6, right_ear),
    ):
        vertical = ear * 0.1 * (640 / 480)
        upper_y = 0.5 - vertical / 2
        lower_y = 0.5 + vertical / 2
        coordinates = (
            (base_x, 0.5),
            (base_x + 0.02, upper_y),
            (base_x + 0.08, upper_y),
            (base_x + 0.1, 0.5),
            (base_x + 0.08, lower_y),
            (base_x + 0.02, lower_y),
        )
        for index, (x, y) in zip(indexes, coordinates):
            landmarks[index] = SimpleNamespace(x=x, y=y)
    return SimpleNamespace(landmark=landmarks)


class SequencedFaceMesh:
    def __init__(self, ears):
        self.ears = iter(ears)

    def process(self, _frame):
        left_ear, right_ear = next(self.ears)
        face = make_face_landmarks(left_ear, right_ear)
        return SimpleNamespace(multi_face_landmarks=[face])


class TestFrameProcessingApi(unittest.TestCase):
    def setUp(self):
        self.originals = {
            "face_mesh": drivesafe.face_mesh,
            "eye_tracker": drivesafe.eye_tracker,
            "monitoring": drivesafe.monitoring,
            "blink_count": drivesafe.blink_count,
            "drowsiness_count": drivesafe.drowsiness_count,
            "incident_active": drivesafe.incident_active,
        }
        drivesafe.app.config["TESTING"] = True
        drivesafe.monitoring = True
        drivesafe.blink_count = 0
        drivesafe.drowsiness_count = 0
        drivesafe.incident_active = False
        self.client = drivesafe.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1

        ok, encoded = cv2.imencode(".jpg", np.zeros((480, 640, 3), dtype=np.uint8))
        self.assertTrue(ok)
        self.frame = encoded.tobytes()

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(drivesafe, name, value)

    def post_frame(self):
        return self.client.post(
            "/process_frame",
            data={"frame": (BytesIO(self.frame), "frame.jpg")},
            content_type="multipart/form-data",
        )

    def test_frame_endpoint_returns_both_eye_ars_and_blink_count(self):
        drivesafe.face_mesh = SequencedFaceMesh([(0.30, 0.28)])
        drivesafe.eye_tracker = SequencedTracker([0.0])

        response = self.post_frame()
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(data["left_eye_ratio"], 0.30, places=2)
        self.assertAlmostEqual(data["right_eye_ratio"], 0.28, places=2)
        self.assertEqual(data["eye_state"], "OPEN")
        self.assertEqual(data["blink_count"], 0)

    def test_frame_endpoint_counts_blink_and_triggers_drowsiness(self):
        # The first three frames form a short blink; the next three keep both eyes closed.
        drivesafe.face_mesh = SequencedFaceMesh(
            [(0.12, 0.13), (0.12, 0.13), (0.30, 0.29),
             (0.12, 0.13), (0.12, 0.13), (0.12, 0.13)]
        )
        drivesafe.eye_tracker = SequencedTracker([0.0, 0.1, 0.2, 1.0, 1.1, 3.1])
        responses = []
        with patch.object(drivesafe, "queue_alert_sms") as queue_sms:
            for _ in range(6):
                responses.append(self.post_frame().get_json())

        self.assertEqual(responses[2]["blink_count"], 1)
        self.assertEqual(responses[5]["driver_state"], "DROWSY")
        self.assertEqual(responses[5]["drowsiness_count"], 1)
        queue_sms.assert_called_once_with(1)


class TestAlertHistoryApi(unittest.TestCase):
    def setUp(self):
        self.client = drivesafe.app.test_client()
        drivesafe.app.config["TESTING"] = True
        with drivesafe.get_db() as connection:
            cursor = connection.execute(
                "INSERT INTO users (full_name, email, password_hash) VALUES (?, ?, ?)",
                ("History Test", "history-test@example.test", "unused"),
            )
            self.user_id = cursor.lastrowid
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

    def tearDown(self):
        with drivesafe.get_db() as connection:
            connection.execute("DELETE FROM alert_history WHERE user_id=?", (self.user_id,))
            connection.execute("DELETE FROM users WHERE id=?", (self.user_id,))

    def test_alert_history_persists_lists_and_clears_user_events(self):
        created = self.client.post(
            "/alerts",
            json={"type": "critical", "message": "Drowsiness detected"},
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.get_json()["message"], "Drowsiness detected")
        listed = self.client.get("/alerts").get_json()["events"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["type"], "critical")
        self.assertEqual(listed[0]["timestamp"], created.get_json()["timestamp"])

        cleared = self.client.delete("/alerts")

        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(self.client.get("/alerts").get_json()["events"], [])

    def test_alert_history_rejects_invalid_events_and_returns_json_when_unauthenticated(self):
        invalid = self.client.post("/alerts", json={"type": "other", "message": "x"})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("error", invalid.get_json())

        with self.client.session_transaction() as session:
            session.clear()
        unauthorized = self.client.post("/sms/settings")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertIn("error", unauthorized.get_json())


if __name__ == "__main__":
    unittest.main()
