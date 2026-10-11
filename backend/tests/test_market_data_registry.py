import unittest
from datetime import datetime, timezone

from backend.config import load_config
from backend.config.catalog import MARKET_DATA_CATALOG
from backend.market_data import (
    UnconfiguredMarketDataProvider,
    build_market_data_provider,
    is_implemented,
)


NOW = datetime(2024, 1, 2, 15, 29, tzinfo=timezone.utc)


class MarketDataRegistryTests(unittest.TestCase):
    def test_no_provider_is_selected_when_nothing_is_connected(self):
        provider = build_market_data_provider(load_config(env={}).market_data)
        self.assertIsInstance(provider, UnconfiguredMarketDataProvider)
        self.assertFalse(provider.get_latest(("AAPL",)))
        self.assertEqual(provider.get_historical("AAPL", NOW, NOW), ())
        self.assertEqual(provider.symbols, ())

    def test_catalog_vendor_with_credentials_resolves_to_a_real_adapter(self):
        for provider_id, descriptor in MARKET_DATA_CATALOG.items():
            with self.subTest(provider_id=provider_id):
                # Supply exactly the credentials this vendor declares, so a
                # newly catalogued provider is held to the same standard.
                settings = load_config(env={
                    "MARKET_DATA_PROVIDER": provider_id,
                    **{name: "present" for name in descriptor.required_credentials},
                }).market_data
                built = build_market_data_provider(settings)
                self.assertNotIsInstance(built, UnconfiguredMarketDataProvider)
                self.assertEqual(built.provider, provider_id)
                self.assertTrue(built.is_configured)
                self.assertTrue(settings.available)
                self.assertTrue(is_implemented(provider_id))

    def test_catalog_vendor_without_credentials_reports_the_missing_key(self):
        for provider_id in MARKET_DATA_CATALOG:
            with self.subTest(provider_id=provider_id):
                settings = load_config(env={
                    "MARKET_DATA_PROVIDER": provider_id,
                }).market_data
                built = build_market_data_provider(settings)
                self.assertIsInstance(built, UnconfiguredMarketDataProvider)
                self.assertIn("no credentials", built.reason)
                self.assertFalse(settings.configured)
                self.assertTrue(is_implemented(provider_id))
                self.assertEqual(built.get_latest(("AAPL",)), ())

    def test_a_catalog_vendor_without_a_registered_adapter_is_not_installed(self):
        from backend.market_data import registry
        removed = registry.IMPLEMENTED.pop("polygon")
        self.addCleanup(registry.IMPLEMENTED.__setitem__, "polygon", removed)
        settings = load_config(env={
            "MARKET_DATA_PROVIDER": "polygon",
            "POLYGON_API_KEY": "present",
        }).market_data
        built = build_market_data_provider(settings)
        self.assertIsInstance(built, UnconfiguredMarketDataProvider)
        self.assertIn("not installed", built.reason)
        self.assertFalse(is_implemented("polygon"))

    def test_a_registered_builder_alone_is_not_enough(self):
        from backend.market_data import registry
        self.addCleanup(registry.IMPLEMENTED.pop, "never_cataloged", None)
        registry.IMPLEMENTED["never_cataloged"] = lambda settings: None
        self.assertFalse(is_implemented("never_cataloged"))
        self.assertFalse(is_implemented("not-even-registered"))

    def test_service_reports_the_honest_provider_state(self):
        from backend.market_data import MarketDataService
        service = MarketDataService(build_market_data_provider(load_config(env={}).market_data))
        self.assertFalse(service.configured)
        self.assertEqual(service.provider_id, "none")
        self.assertEqual(service.status["state"], "NOT_CONFIGURED")
        self.assertEqual(service.get_latest(("AAPL",)), ())


if __name__ == "__main__":
    unittest.main()
