import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import openclaw_k


class ClawctlApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = "test-token"
        self.client = TestClient(openclaw_k.create_api_app(self.token))
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def test_health_is_public(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)

    def test_missing_auth_rejected(self) -> None:
        response = self.client.get("/v1/users")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "auth_missing")

    def test_wrong_auth_rejected(self) -> None:
        response = self.client.get("/v1/users", headers={"Authorization": "Bearer nope"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "auth_forbidden")

    @patch("openclaw_k.list_users_service")
    def test_list_users_success(self, mock_list_users_service) -> None:
        mock_list_users_service.return_value = [
            {
                "user": "alice",
                "container": "openclaw-alice",
                "status": "running",
                "health": "healthy",
                "ready": True,
                "port": 19001,
            }
        ]
        response = self.client.get("/v1/users", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["user"], "alice")

    @patch("openclaw_k.create_user_service")
    def test_create_user_success(self, mock_create_user_service) -> None:
        mock_create_user_service.return_value = {
            "user": "alice",
            "container": "openclaw-alice",
            "status": "ready",
            "port": 19001,
            "url": "http://127.0.0.1:19001/",
            "connect_link": "http://127.0.0.1:19001/#token=abc",
            "token": "abc",
            "image": openclaw_k.DEFAULT_OPENCLAW_IMAGE,
            "config_ingested": True,
            "config_file_path": "/tmp/openclaw.json",
        }
        response = self.client.post(
            "/v1/users",
            headers=self.auth,
            json={"username": "alice", "port": 19001},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "ready")

    def test_create_user_invalid_payload(self) -> None:
        response = self.client.post(
            "/v1/users",
            headers=self.auth,
            json={"username": "alice"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    @patch("openclaw_k.create_user_service")
    def test_create_user_conflict(self, mock_create_user_service) -> None:
        mock_create_user_service.side_effect = openclaw_k.ServiceError(409, "user_exists", "already exists")
        response = self.client.post(
            "/v1/users",
            headers=self.auth,
            json={"username": "alice", "port": 19001},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "user_exists")

    @patch("openclaw_k.update_all_service")
    def test_update_all_success(self, mock_update_all_service) -> None:
        mock_update_all_service.return_value = {
            "ok": True,
            "total": 1,
            "updated": 1,
            "failed": 0,
            "items": [
                {
                    "user": "alice",
                    "container": "openclaw-alice",
                    "updated": True,
                    "restarted": True,
                    "ready": True,
                    "applied": {"config": True, "skills": True, "soul": False},
                    "errors": [],
                }
            ],
        }
        response = self.client.post(
            "/v1/update/all",
            headers=self.auth,
            json={},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated"], 1)

    @patch("openclaw_k.update_all_service")
    def test_update_all_all_failed(self, mock_update_all_service) -> None:
        mock_update_all_service.side_effect = openclaw_k.ServiceError(
            500,
            "all_updates_failed",
            "Failed to update all targeted containers.",
            {"ok": False, "total": 1, "updated": 0, "failed": 1, "items": []},
        )
        response = self.client.post(
            "/v1/update/all",
            headers=self.auth,
            json={},
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "all_updates_failed")


if __name__ == "__main__":
    unittest.main()
