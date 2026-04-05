import unittest
from types import SimpleNamespace
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

import openclaw_k


class _FakeContainer:
    def __init__(self, name: str, user: str) -> None:
        self.name = name
        self.labels = {"managed-by": "openclaw-k", "openclaw-k.user": user}
        self.attrs = {"State": {"Health": {"Status": "healthy"}}}
        self.status = "running"

    def reload(self) -> None:
        return None

    def exec_run(self, *_args, **_kwargs):
        return SimpleNamespace(exit_code=0)

    def restart(self) -> None:
        return None


class UpdateAllServiceTests(unittest.TestCase):
    @patch("openclaw_k.get_docker_client")
    def test_update_all_no_running_targets(self, mock_get_docker_client) -> None:
        client = MagicMock()
        client.containers.list.return_value = []
        mock_get_docker_client.return_value = client

        with self.assertRaises(openclaw_k.ServiceError) as cm:
            openclaw_k.update_all_service()
        self.assertEqual(cm.exception.code, "no_running_users")

    @patch("openclaw_k.wait_until_ready")
    @patch("openclaw_k._sync_workspace_mirror")
    @patch("openclaw_k._sync_skills_mirror")
    @patch("openclaw_k.put_file_into_container")
    @patch("openclaw_k.resolve_optional_defaults")
    @patch("openclaw_k.resolve_config_file_path")
    @patch("openclaw_k.get_docker_client")
    def test_update_all_continue_on_error(
        self,
        mock_get_docker_client,
        mock_resolve_config_file_path,
        mock_resolve_optional_defaults,
        mock_put_file_into_container,
        mock_sync_skills_mirror,
        mock_sync_workspace_mirror,
        mock_wait_until_ready,
    ) -> None:
        c1 = _FakeContainer("openclaw-a", "a")
        c2 = _FakeContainer("openclaw-b", "b")
        client = MagicMock()
        client.containers.list.return_value = [c1, c2]
        mock_get_docker_client.return_value = client
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            config_file = td_path / "openclaw.json"
            config_file.write_text("{}")
            soul_file = td_path / "SOUL.md"
            soul_file.write_text("soul")
            skills_dir = td_path / "skills"
            skills_dir.mkdir()
            mock_resolve_config_file_path.return_value = config_file
            workspace_dir = td_path / "workspace"
            workspace_dir.mkdir()
            mock_resolve_optional_defaults.return_value = (skills_dir, soul_file, workspace_dir)

            def maybe_fail(container, *_args, **_kwargs):
                if container.name == "openclaw-b":
                    raise openclaw_k.ServiceError(500, "copy_failed", "copy failed")
                return None

            mock_put_file_into_container.side_effect = maybe_fail
            mock_sync_skills_mirror.return_value = None
            mock_sync_workspace_mirror.return_value = None
            mock_wait_until_ready.return_value = None

            result = openclaw_k.update_all_service()
            self.assertEqual(result["total"], 2)
            self.assertEqual(result["updated"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(len(result["items"]), 2)


if __name__ == "__main__":
    unittest.main()
