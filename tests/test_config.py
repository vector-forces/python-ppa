import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

# Bootstrapping configuration never needs a live database.
with patch.dict(os.environ, {"mongoURI": "mongodb://localhost:27017"}):
    from ppa import config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.resources = patch.object(config, "RESOURCES_DIR", self.root)
        self.resources.start()
        self.addCleanup(self.resources.stop)

    def write(self, filename, content):
        (self.root / filename).write_text(content)

    def test_single_yaml_and_mongo_uri_alias(self):
        self.write("settings.yaml", "mongodb:\n  database: test\n")
        with patch.dict(os.environ, {"mongoURI": "mongodb://localhost:27017"}):
            settings = config.ConfigSettings()
        self.assertEqual(settings.database_name, "test")
        self.assertEqual(settings.mongo_uri, "mongodb://localhost:27017")

    def test_legacy_postgres_profile(self):
        self.write("settings.yml", "app:\n  profile: dev\n")
        self.write("settings-dev.yml", "postgresql:\n  dsn: postgresql://localhost/test\n")
        settings = config.ConfigSettings()
        self.assertEqual(settings.postgres_dsn, "postgresql://localhost/test")

    def test_recursive_profile_override_and_yaml_precedence(self):
        self.write("settings.yaml", "mongodb:\n  uri: mongodb://localhost\n  database: test\n  options:\n    maxPoolSize: 10\n    retryReads: true\n")
        self.write("settings.yml", "invalid: legacy file should not be selected\n")
        self.write("settings-dev.yaml", "mongodb:\n  options:\n    maxPoolSize: 20\n")
        settings = config.ConfigSettings()
        self.assertEqual(settings.database_name, "test")
        self.assertEqual(settings.mongo_options, {"maxPoolSize": 20, "retryReads": True})

    def test_environment_cannot_inject_yaml_keys(self):
        self.write("settings.yaml", "secret: ${TEST_SECRET}\noptions:\n  enabled: ${TEST_ENABLED:false}\n  size: ${TEST_SIZE:10}\n")
        secret = 'quotes: "\nadmin: true # preserved as data'
        with patch.dict(os.environ, {"TEST_SECRET": secret, "TEST_ENABLED": "false"}):
            result = config._load_and_interpolate_yaml(self.root / "settings.yaml")
        self.assertEqual(result, {"secret": secret, "options": {"enabled": False, "size": 10}})

    def test_incomplete_mongo_config_fails(self):
        self.write("settings.yaml", "mongodb:\n  uri: mongodb://localhost\n")
        with self.assertRaises(KeyError):
            config.ConfigSettings()

    def test_explicit_root_supports_both_layouts(self):
        with patch.dict(os.environ, {"PROJECT_ROOT": str(self.root)}):
            self.assertEqual(config._detect_resources_dir(), self.root / "resources")
            self.write("settings.yaml", "app: {}\n")
            self.assertEqual(config._detect_resources_dir(), self.root)


class MongoLifecycleTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("_mongo_client", None), ("db", None)):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.factory = patch.object(config, "MongoClient")
        self.client_factory = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.options = patch.object(config.settings, "mongo_options", {"serverSelectionTimeoutMS": 123})
        self.options.start()
        self.addCleanup(self.options.stop)

    def test_concurrent_initialization_uses_one_pool_and_options(self):
        with ThreadPoolExecutor(max_workers=8) as workers:
            clients = list(workers.map(lambda _: config._get_mongo_client(), range(32)))
        self.client_factory.assert_called_once_with(config.settings.mongo_uri, serverSelectionTimeoutMS=123)
        self.assertTrue(all(client is clients[0] for client in clients))

    def test_close_is_idempotent_and_new_collection_uses_fresh_client(self):
        first, second = MagicMock(), MagicMock()
        self.client_factory.side_effect = [first, second]
        config.get_collection("users")
        config.close_db_connection()
        config.close_db_connection()
        first.close.assert_called_once()
        self.assertIsNone(config.db)
        config.get_collection("users")
        self.assertIs(config._mongo_client, second)
        self.assertEqual(self.client_factory.call_count, 2)

    def test_ping_propagates_outage_and_reuses_client_after_recovery(self):
        from pymongo.errors import AutoReconnect

        client = self.client_factory.return_value
        client.admin.command.side_effect = [AutoReconnect("offline"), {"ok": 1}]
        with self.assertRaises(AutoReconnect):
            config.ping_mongo()
        config.ping_mongo()
        self.client_factory.assert_called_once()
        self.assertEqual(client.admin.command.call_count, 2)


if __name__ == "__main__":
    unittest.main()
