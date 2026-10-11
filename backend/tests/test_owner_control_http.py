"""Owner-control authorization over HTTP: every privileged route, both refusals.

The control plane is the most dangerous surface in the product, so this module
proves the guard at the HTTP boundary rather than trusting the UI:

- every control route refuses a **missing** token with 401 and changes nothing;
- every control route refuses a **wrong** token with 400/401 and changes nothing;
- the configured token really performs the action (start/pause/resume/stop,
  emergency stop + reset, terminate, configuration update);
- the emergency stop latches risk and the orchestrator until an owner reset;
- when `OWNER_CONTROL_TOKEN` is unset the routes answer 503 and nothing is
  privileged by default;
- no response body ever echoes the owner token.
"""
from __future__ import annotations

import json
import threading
import unittest
from urllib import request as urlrequest
from urllib.error import HTTPError

from backend.core.runtime import ApplicationRuntime

OWNER_TOKEN = "owner-http-test-token"
CONTROL_ROUTES = (
    "/api/control/start",
    "/api/control/stop",
    "/api/control/pause",
    "/api/control/resume",
    "/api/control/emergency-stop",
    "/api/control/reset-emergency-stop",
    "/api/control/terminate",
    "/api/control/configuration",
)


class OwnerControlHttpTests(unittest.TestCase):
    def setUp(self):
        self.runtime = ApplicationRuntime(database_path=":memory:",
                                          owner_token=OWNER_TOKEN)
        self.server = _serve(self.runtime)
        self.port = self.server.server_address[1]

    def tearDown(self):
        _close(self.server, self.runtime)

    def call(self, path, body=None, token=OWNER_TOKEN):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Owner-Token"] = token
        data = json.dumps(body or {}).encode("utf-8")
        http_request = urlrequest.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers,
            method="POST")
        try:
            with urlrequest.urlopen(http_request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            payload = error.read().decode("utf-8")
            try:
                return error.code, json.loads(payload)
            except json.JSONDecodeError:
                return error.code, {"raw": payload}

    def orchestrator_status(self) -> str:
        return self.runtime.orchestrator.state.status.value

    # -- negative authorization ------------------------------------------
    def test_every_control_route_refuses_a_missing_token(self):
        for path in CONTROL_ROUTES:
            with self.subTest(path=path):
                before = self.orchestrator_status()
                status, body = self.call(path, token=None)
                self.assertEqual(status, 401, body)
                self.assertEqual(self.orchestrator_status(), before,
                                 f"{path} changed state without a token")
        self.assertFalse(self.runtime.risk.kill_switch_enabled)

    def test_every_control_route_refuses_a_wrong_token(self):
        for path in CONTROL_ROUTES:
            with self.subTest(path=path):
                before = self.orchestrator_status()
                status, body = self.call(path, token="not-the-owner-token")
                self.assertEqual(status, 401, body)
                self.assertEqual(self.orchestrator_status(), before)
        self.assertFalse(self.runtime.risk.kill_switch_enabled)

    def test_a_direct_api_call_is_the_same_guard_as_the_dashboard(self):
        # The UI hides nothing that the API would otherwise allow.
        status, _ = self.call("/api/control/emergency-stop",
                              {"reason": "no token supplied"}, token=None)
        self.assertEqual(status, 401)
        status, body = self.call("/api/control/emergency-stop",
                                 {"reason": "wrong token"}, token="guess")
        self.assertEqual(status, 401)
        self.assertIn("owner authentication failed", body.get("error", ""))

    def test_the_owner_token_is_never_echoed_back(self):
        for token in (None, "wrong", OWNER_TOKEN):
            for path in CONTROL_ROUTES:
                with self.subTest(path=path, token=token):
                    _, body = self.call(path, token=token)
                    serialized = json.dumps(body)
                    self.assertNotIn(OWNER_TOKEN, serialized)
                    self.assertNotIn(OWNER_TOKEN.upper(), serialized.upper())

    # -- positive authorization ------------------------------------------
    def test_the_configured_token_drives_pause_resume_and_stop(self):
        status, _ = self.call("/api/control/pause")
        self.assertEqual(status, 200)
        self.assertEqual(self.orchestrator_status(), "PAUSED")
        status, _ = self.call("/api/control/resume")
        self.assertEqual(status, 200)
        self.assertEqual(self.orchestrator_status(), "RUNNING")
        status, _ = self.call("/api/control/stop")
        self.assertEqual(status, 200)
        self.assertEqual(self.orchestrator_status(), "STOPPED")

    def test_emergency_stop_latches_until_an_owner_reset(self):
        status, body = self.call("/api/control/emergency-stop", {"reason": "drill"})
        self.assertEqual(status, 200, body)
        self.assertTrue(self.runtime.risk.kill_switch_enabled)
        self.assertEqual(self.orchestrator_status(), "EMERGENCY_STOPPED")
        blocked = self.runtime.orchestrator.run_once()
        self.assertEqual(blocked.cycle, self.runtime.orchestrator.state.current_cycle,
                         "an emergency-stopped orchestrator must not run a cycle")

        # Even the correct token cannot start trading while it is latched.
        self.call("/api/control/start")
        self.assertEqual(self.orchestrator_status(), "EMERGENCY_STOPPED")
        self.assertTrue(self.runtime.risk.kill_switch_enabled)

        status, _ = self.call("/api/control/reset-emergency-stop",
                              {"reason": "drill finished"})
        self.assertEqual(status, 200)
        self.assertFalse(self.runtime.risk.kill_switch_enabled)

    def test_terminate_only_shuts_the_process_down_with_the_owner_token(self):
        called = []
        self.runtime.owner_controls._shutdown = lambda: called.append(True)
        status, _ = self.call("/api/control/terminate", token="wrong")
        self.assertEqual(called, [])
        status, _ = self.call("/api/control/terminate")
        self.assertEqual(status, 200)
        self.assertTrue(called)

    def test_configuration_updates_are_owner_guarded_and_validated(self):
        status, _ = self.call("/api/control/configuration",
                              {"key": "scan_interval_seconds", "value": 42},
                              token=None)
        self.assertEqual(status, 401)

        status, body = self.call("/api/control/configuration",
                                 {"key": "scan_interval_seconds", "value": 0.5})
        self.assertEqual(status, 200, body)
        self.assertEqual(
            self.runtime.orchestrator.config.scan_interval.total_seconds(), 0.5)

        recorded = [row for row in self.runtime.repository.events()
                    if row["event_type"] == "RUNTIME_CONFIG_UPDATED"]
        self.assertTrue(recorded, "an approved configuration change must be journaled")
        self.assertEqual(recorded[0]["context_json"]["scan_interval"], 0.5,
                         "the journal keeps the exact interval that was set")

        status, _ = self.call("/api/control/configuration",
                              {"key": "max_candidate_price", "value": 101})
        self.assertEqual(status, 400, "an out-of-range value must be refused")

    def test_sensitive_actions_are_audited(self):
        self.call("/api/control/emergency-stop", {"reason": "audit drill"})
        events = self.runtime.repository.events()
        recorded = [row for row in events
                    if row["event_type"] == "OWNER_EMERGENCY_STOP"]
        self.assertTrue(recorded, "an emergency stop must be journaled")
        context = recorded[0]["context_json"]
        self.assertEqual(context["reason"], "audit drill")
        self.assertNotIn(OWNER_TOKEN, json.dumps(recorded))


class OwnerControlsDisabledTests(unittest.TestCase):
    def setUp(self):
        self.runtime = ApplicationRuntime(database_path=":memory:", owner_token=None)
        self.server = _serve(self.runtime)
        self.port = self.server.server_address[1]

    def tearDown(self):
        _close(self.server, self.runtime)

    def test_control_routes_explain_that_owner_controls_are_disabled(self):
        for path in CONTROL_ROUTES:
            with self.subTest(path=path):
                request = urlrequest.Request(
                    f"http://127.0.0.1:{self.port}{path}", data=b"{}",
                    headers={"Content-Type": "application/json",
                             "X-Owner-Token": "anything"},
                    method="POST")
                try:
                    with urlrequest.urlopen(request, timeout=10) as response:
                        status, body = response.status, response.read().decode()
                except HTTPError as error:
                    status, body = error.code, error.read().decode()
                self.assertEqual(status, 503)
                self.assertIn("owner controls are disabled", body)
        self.assertFalse(self.runtime.risk.kill_switch_enabled)

    def test_status_admits_owner_controls_are_disabled(self):
        with urlrequest.urlopen(f"http://127.0.0.1:{self.port}/api/status",
                                timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertFalse(payload["owner_controls_enabled"])


def _serve(runtime):
    from backend.core.server import create_server
    server = create_server(runtime, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _close(server, runtime):
    server.shutdown()
    server.server_close()
    runtime.close()


if __name__ == "__main__":
    unittest.main()
