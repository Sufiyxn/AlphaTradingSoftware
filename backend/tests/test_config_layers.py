import json
import os
import unittest
from unittest.mock import patch

from backend.config import (
    MARKET_DATA_CATALOG,
    Config,
    load_config,
    load_settings,
)
from backend.config.catalog import CATALOGS, is_known
from backend.modes import TradingMode

SENTINEL = "sentinel-secret-value-must-never-leak"

RISK_ENV = {
    "RISK_MIN_REWARD_RISK": "1.5",
    "RISK_FRACTION": "0.5",
    "RISK_MAX_DAILY_LOSS_FRACTION": "0.5",
    "STRATEGY_MIN_REWARD_RISK": "1.0",
}


def build(env: dict) -> Config:
    return load_config(env=env)


class ConfigurationLayerTests(unittest.TestCase):
    def test_defaults_with_empty_environment(self):
        config = build({})
        self.assertEqual(config.app.host, "127.0.0.1")
        self.assertEqual(config.app.port, 8000)
        self.assertEqual(config.app.network_exposure, "LOCAL")
        self.assertEqual(config.trading.mode, TradingMode.PAPER)
        self.assertEqual(config.trading.max_candidate_price, 100.0)
        self.assertTrue(config.trading.universe)
        self.assertEqual(config.market_data.provider, "none")
        self.assertFalse(config.market_data.configured)
        self.assertEqual(config.broker.provider, "none")
        self.assertFalse(config.broker.configured)
        self.assertFalse(config.tradingview.configured)
        self.assertEqual(config.ai.provider, "local")
        self.assertTrue(config.ai.active)

    def test_legacy_development_provider_values_resolve_to_not_configured(self):
        for value in ("MOCK", "mock", "TEST", "FIXTURE", ""):
            config = build({"MARKET_DATA_PROVIDER": value})
            self.assertEqual(config.market_data.provider, "none", value)
            self.assertFalse(config.market_data.configured, value)
        for value in ("mock", "MOCK"):
            config = build({"TRADING_PLATFORM_ADAPTER": value})
            self.assertEqual(config.broker.provider, "none", value)
            self.assertFalse(config.broker.configured, value)

    def test_mock_fixture_is_never_a_selectable_product_integration(self):
        from backend.config.catalog import is_selectable
        self.assertNotIn("mock", MARKET_DATA_CATALOG)
        self.assertFalse(is_known("market_data", "mock"))
        self.assertFalse(is_selectable("market_data", "mock"))

    def test_configured_provider_reports_presence_without_secrets(self):
        config = build({
            "MARKET_DATA_PROVIDER": "polygon",
            "POLYGON_API_KEY": SENTINEL,
            "MARKET_DATA_BASE_URL": "https://example.invalid",
        })
        self.assertTrue(config.market_data.configured)
        self.assertEqual(config.market_data.missing_credentials, ())
        self.assertEqual(config.market_data.state, "NOT_CONFIGURED")
        self.assertFalse(config.market_data.available)
        self.assertIn("not installed", config.market_data.unavailable_reason)
        self.assertNotIn(SENTINEL, json.dumps(config.describe()))

    def test_unconfigured_provider_reports_missing_credential_names(self):
        config = build({"MARKET_DATA_PROVIDER": "polygon"})
        self.assertFalse(config.market_data.configured)
        self.assertEqual(config.market_data.missing_credentials, ("POLYGON_API_KEY",))

    def test_unknown_provider_is_never_configured(self):
        config = build({"MARKET_DATA_PROVIDER": "not-a-real-vendor"})
        self.assertTrue(config.market_data.known is False)
        self.assertFalse(config.market_data.configured)

    def test_broker_environment_validation(self):
        sandbox = build({"TRADING_PLATFORM_PROVIDER": "alpaca",
                         "TRADING_PLATFORM_ENVIRONMENT": "SANDBOX",
                         "ALPACA_API_KEY": SENTINEL, "ALPACA_API_SECRET": SENTINEL})
        self.assertTrue(sandbox.broker.configured)
        self.assertEqual(sandbox.broker.environment, "SANDBOX")

        live = build({"TRADING_PLATFORM_PROVIDER": "alpaca",
                      "TRADING_PLATFORM_ENVIRONMENT": "LIVE",
                      "ALPACA_API_KEY": SENTINEL, "ALPACA_API_SECRET": SENTINEL})
        self.assertTrue(live.broker.configured)
        self.assertEqual(live.broker.environment, "LIVE")

        invalid = build({"TRADING_PLATFORM_PROVIDER": "alpaca",
                         "TRADING_PLATFORM_ENVIRONMENT": "PRODUCTION",
                         "ALPACA_API_KEY": SENTINEL, "ALPACA_API_SECRET": SENTINEL})
        self.assertFalse(invalid.broker.configured)
        self.assertFalse(invalid.broker.environment_valid)

    def test_no_secret_value_appears_anywhere_in_described_configuration(self):
        config = build({
            "MARKET_DATA_PROVIDER": "polygon",
            "POLYGON_API_KEY": SENTINEL,
            "TRADING_PLATFORM_PROVIDER": "alpaca",
            "ALPACA_API_KEY": SENTINEL,
            "ALPACA_API_SECRET": SENTINEL,
            "TRADINGVIEW_WEBHOOK_SECRET": SENTINEL,
            "AI_API_KEY": SENTINEL,
            "NOTIFICATION_WEBHOOK_URL": f"https://example.invalid/hook/{SENTINEL}",
            "DISCORD_WEBHOOK_URL": f"https://example.invalid/discord/{SENTINEL}",
            "TELEGRAM_BOT_TOKEN": SENTINEL,
            "TELEGRAM_CHAT_ID": "12345",
            "OWNER_CONTROL_TOKEN": SENTINEL,
        })
        serialized = json.dumps(config.describe())
        self.assertNotIn(SENTINEL, serialized)
        self.assertNotIn(SENTINEL, json.dumps(config.app.describe()))
        self.assertIn("owner_controls_enabled", config.app.describe())
        self.assertNotIn("owner_token", config.app.describe())

    def test_risk_and_strategy_floors_cannot_be_loosened_by_configuration(self):
        for key in RISK_ENV:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    build({key: RISK_ENV[key]})

    def test_candidate_price_ceiling_cannot_exceed_one_hundred(self):
        self.assertEqual(build({"MAX_CANDIDATE_PRICE": "25"}).trading.max_candidate_price, 25.0)
        with self.assertRaises(ValueError):
            build({"MAX_CANDIDATE_PRICE": "150"})
        with self.assertRaises(ValueError):
            build({"MAX_CANDIDATE_PRICE": "0"})

    def test_live_mode_intent_requires_both_settings(self):
        self.assertEqual(build({}).mode_selection.effective, TradingMode.PAPER)
        self.assertEqual(
            build({"TRADING_MODE": "LIVE"}).mode_selection.effective, TradingMode.PAPER)
        self.assertEqual(
            build({"TRADING_MODE": "LIVE", "LIVE_TRADING_ENABLED": "true"})
            .mode_selection.effective, TradingMode.LIVE)
        self.assertEqual(
            build({"TRADING_MODE": "BACKTEST"}).mode_selection.effective, TradingMode.BACKTEST)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            build({"TRADING_MODE": "SWING"})

    def test_watchlist_is_normalised_and_deduplicated(self):
        config = build({"UNIVERSE": "aapl, aapl; MSFT ,  "})
        self.assertEqual(config.trading.universe, ("AAPL", "MSFT"))
        with self.assertRaises(ValueError):
            build({"UNIVERSE": ", , "})

    def test_ai_provider_aliases_normalise_to_catalog_ids(self):
        self.assertEqual(build({"AI_PROVIDER": "LOCAL/MOCK"}).ai.provider, "local")
        self.assertEqual(build({"AI_PROVIDER": "MOCK"}).ai.provider, "local")
        self.assertEqual(build({"AI_PROVIDER": "NONE"}).ai.provider, "none")
        self.assertFalse(build({"AI_PROVIDER": "NONE"}).ai.active)

    def test_unimplemented_ai_provider_is_never_active(self):
        config = build({"AI_PROVIDER": "openai_compatible", "AI_API_KEY": SENTINEL})
        self.assertTrue(config.ai.configured)
        self.assertFalse(config.ai.implemented)
        self.assertFalse(config.ai.active)
        self.assertEqual(config.ai.state, "NOT_CONFIGURED")
        self.assertIn("not installed", config.ai.describe()["reason"])
        self.assertNotIn(SENTINEL, json.dumps(config.describe()))

    def test_notification_channels_are_independent(self):
        config = build({"DISCORD_WEBHOOK_URL": "https://example.invalid/x"})
        channels = {channel.provider: channel for channel in config.notifications.channels}
        self.assertTrue(channels["local"].active)
        self.assertTrue(channels["discord"].configured)
        self.assertFalse(channels["discord"].implemented)
        self.assertFalse(channels["discord"].active)
        self.assertFalse(channels["telegram"].configured)
        self.assertFalse(channels["webhook"].configured)
        self.assertEqual(config.notifications.describe()["active_channels"], ["local"])
        self.assertEqual(config.notifications.state, "CONFIGURED")

    def test_no_catalog_claims_an_adapter_this_build_cannot_use(self):
        in_process = {("ai", "none"), ("ai", "local"), ("notification", "local")}
        for kind, catalog in CATALOGS.items():
            for integration_id, found in catalog.items():
                with self.subTest(kind=kind, integration_id=integration_id):
                    if (kind, integration_id) in in_process:
                        self.assertTrue(found.implemented)
                    else:
                        self.assertFalse(
                            found.implemented,
                            f"{kind}/{integration_id} is advertised as implemented but no adapter exists")

    def test_process_environment_is_never_overwritten_by_the_env_file(self):
        with patch.dict(os.environ, {"APP_PORT": "8123"}, clear=True):
            from backend.config import load_local_env
            load_local_env()
            self.assertEqual(os.environ["APP_PORT"], "8123")
            self.assertEqual(load_settings().port, 8123)

    def test_back_compatible_settings_loader(self):
        with patch.dict(os.environ, {"APP_HOST": "0.0.0.0", "APP_PORT": "8123",
                                     "APP_ENV": "test"}, clear=True):
            settings = load_settings()
        self.assertEqual((settings.host, settings.port, settings.environment),
                         ("0.0.0.0", 8123, "test"))
        self.assertEqual(settings.network_exposure, "NON_LOCALHOST")

    def test_invalid_port_is_rejected(self):
        with self.assertRaises(ValueError):
            build({"APP_PORT": "not-a-number"})
        with self.assertRaises(ValueError):
            build({"APP_PORT": "70000"})


if __name__ == "__main__":
    unittest.main()
