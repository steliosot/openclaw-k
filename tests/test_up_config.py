import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import openclaw_k


class UpConfigTests(unittest.TestCase):
    def test_load_up_config_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "openclaw-k.yaml"
            provider = Path(td) / "openclaw.json"
            provider.write_text("{}")
            cfg.write_text(
                """
api:
  host: 0.0.0.0
  port: 8787
docker:
  container_name: openclaw-k-api
  image_tag: openclaw-k:local
providers:
  default: gemma
  profiles:
    gemma:
      file: ./openclaw.json
defaults:
  publish_bind_ip: 0.0.0.0
  connect_host: 34.16.127.240
""".strip()
            )
            model, path = openclaw_k.load_up_config(str(cfg))
            self.assertEqual(path, cfg.resolve())
            self.assertEqual(model.providers.default, "gemma")
            self.assertEqual(model.api.port, 8787)

    def test_load_up_config_invalid_default_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "openclaw-k.yaml"
            cfg.write_text(
                """
providers:
  default: missing
  profiles:
    gemma:
      file: ./openclaw.json
""".strip()
            )
            with self.assertRaises(openclaw_k.ServiceError):
                openclaw_k.load_up_config(str(cfg))

    def test_resolve_config_file_path_uses_openclaw_k_yaml_default_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            provider = td_path / "openclaw-openai.json"
            provider.write_text('{"ok": true}')
            cfg = td_path / "openclaw-k.yaml"
            cfg.write_text(
                """
providers:
  default: openai
  profiles:
    openai:
      file: ./openclaw-openai.json
""".strip()
            )
            cwd = Path.cwd()
            os.chdir(td_path)
            try:
                resolved = openclaw_k.resolve_config_file_path(None)
            finally:
                os.chdir(cwd)

            self.assertEqual(resolved, provider.resolve())

    def test_resolve_config_file_path_with_provider_alias(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            provider_openai = td_path / "openclaw-openai.json"
            provider_openai.write_text('{"ok": true}')
            provider_gemma = td_path / "openclaw-gemma4.json"
            provider_gemma.write_text('{"ok": true}')
            cfg = td_path / "openclaw-k.yaml"
            cfg.write_text(
                """
providers:
  default: openai
  profiles:
    openai:
      file: ./openclaw-openai.json
    gemma:
      file: ./openclaw-gemma4.json
""".strip()
            )
            cwd = Path.cwd()
            old_profiles_env = os.environ.get(openclaw_k.PROVIDER_PROFILES_ENV)
            os.chdir(td_path)
            try:
                os.environ.pop(openclaw_k.PROVIDER_PROFILES_ENV, None)
                resolved_openai = openclaw_k.resolve_config_file_path(None, "openclaw-openai")
                resolved_gemma = openclaw_k.resolve_config_file_path(None, "openclaw-gemma4")
            finally:
                os.chdir(cwd)
                if old_profiles_env is None:
                    os.environ.pop(openclaw_k.PROVIDER_PROFILES_ENV, None)
                else:
                    os.environ[openclaw_k.PROVIDER_PROFILES_ENV] = old_profiles_env

            self.assertEqual(resolved_openai, provider_openai.resolve())
            self.assertEqual(resolved_gemma, provider_gemma.resolve())

    @patch("openclaw_k.wait_for_api_health")
    @patch("openclaw_k.get_docker_client")
    def test_up_cli_build_and_run(self, mock_get_client, mock_wait_for_api_health) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg = td_path / "openclaw-k.yaml"
            provider = td_path / "openclaw.json"
            provider.write_text("{}")
            cfg.write_text(
                """
api:
  host: 0.0.0.0
  port: 8787
docker:
  container_name: openclaw-k-api
  image_tag: openclaw-k:local
providers:
  default: gemma
  profiles:
    gemma:
      file: ./openclaw.json
defaults:
  publish_bind_ip: 0.0.0.0
  connect_host: 34.16.127.240
""".strip()
            )

            mock_client = MagicMock()
            mock_client.containers.get.side_effect = openclaw_k.NotFound("not found")
            mock_get_client.return_value = mock_client

            old = os.environ.get("OPENCLAW_K_API_TOKEN")
            os.environ["OPENCLAW_K_API_TOKEN"] = "test-token"
            try:
                openclaw_k.up_cli(Namespace(config=str(cfg), no_build=False))
            finally:
                if old is None:
                    del os.environ["OPENCLAW_K_API_TOKEN"]
                else:
                    os.environ["OPENCLAW_K_API_TOKEN"] = old

            self.assertTrue(mock_client.images.build.called)
            self.assertTrue(mock_client.containers.run.called)
            kwargs = mock_client.containers.run.call_args.kwargs
            self.assertEqual(kwargs["name"], "openclaw-k-api")
            self.assertIn("OPENCLAW_K_DEFAULT_PROVIDER_FILE", kwargs["environment"])
            self.assertIn("OPENCLAW_K_PROVIDER_PROFILES_JSON", kwargs["environment"])
            mock_wait_for_api_health.assert_called_once()

    @patch("uvicorn.run")
    def test_api_serve_cli_loads_defaults_from_yaml(self, mock_uvicorn_run) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg = td_path / "openclaw-k.yaml"
            provider = td_path / "openclaw-openai.json"
            provider.write_text("{}")
            cfg.write_text(
                """
api:
  host: 0.0.0.0
  port: 8787
providers:
  default: openai
  profiles:
    openai:
      file: ./openclaw-openai.json
defaults:
  publish_bind_ip: 0.0.0.0
  connect_host: 34.16.127.240
""".strip()
            )

            old_token = os.environ.get("OPENCLAW_K_API_TOKEN")
            old_provider = os.environ.get(openclaw_k.DEFAULT_PROVIDER_FILE_ENV)
            try:
                os.environ["OPENCLAW_K_API_TOKEN"] = "test-token"
                openclaw_k.api_serve_cli(Namespace(host="0.0.0.0", port=8787, token=None, config=str(cfg)))
                resolved_provider = os.environ.get(openclaw_k.DEFAULT_PROVIDER_FILE_ENV)
            finally:
                if old_token is None:
                    os.environ.pop("OPENCLAW_K_API_TOKEN", None)
                else:
                    os.environ["OPENCLAW_K_API_TOKEN"] = old_token
                if old_provider is None:
                    os.environ.pop(openclaw_k.DEFAULT_PROVIDER_FILE_ENV, None)
                else:
                    os.environ[openclaw_k.DEFAULT_PROVIDER_FILE_ENV] = old_provider

            self.assertEqual(resolved_provider, str(provider.resolve()))
            mock_uvicorn_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
