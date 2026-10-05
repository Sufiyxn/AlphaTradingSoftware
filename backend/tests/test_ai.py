import unittest
from datetime import datetime, timezone

from backend.ai import AIAnalysisService, AIRecommendation, MockAIAnalysisProvider
from backend.market_data import Bar
from backend.strategies.models import Direction, TradeSetup


NOW = datetime(2024, 1, 2, 15, tzinfo=timezone.utc)


def setup(symbol="ABC", ratio=2.0):
    return TradeSetup(symbol, Direction.LONG, 50, 47.5, 50 + 2.5 * ratio,
                      2.5, 2.5 * ratio, ratio, NOW, "test-strategy", .5, "fixture")


class AIAnalysisTests(unittest.TestCase):
    def test_mock_analysis_is_advisory_deterministic_and_keyless(self):
        service = AIAnalysisService(MockAIAnalysisProvider())
        bars = (Bar("ABC", NOW, 50, 51, 49, 50, 100),)
        first = service.analyze(setup(), bars)
        self.assertEqual(first, service.analyze(setup(), bars))
        self.assertEqual(first.recommendation, AIRecommendation.SUPPORT)
        self.assertEqual(first.symbol, "ABC")
        self.assertGreaterEqual(first.score, 0)
        self.assertLessEqual(first.score, 1)
        self.assertEqual(first.market_context_notes, ("Reviewed 1 normalized market bars",))

    def test_reject_advice_and_symbol_mismatch_warning(self):
        result = AIAnalysisService(MockAIAnalysisProvider()).analyze(
            setup("ABC", 1.5), (Bar("XYZ", NOW, 50, 51, 49, 50, 100),))
        self.assertEqual(result.recommendation, AIRecommendation.REJECT)
        self.assertIn("reward/risk is below 2:1", result.warnings)
        self.assertIn("market context symbol does not match setup", result.warnings)

    def test_service_rejects_provider_output_for_another_symbol(self):
        class WrongSymbol:
            def analyze(self, trade_setup, market_context=()):
                result = MockAIAnalysisProvider().analyze(trade_setup, market_context)
                from dataclasses import replace
                return replace(result, symbol="XYZ")

        with self.assertRaisesRegex(ValueError, "unexpected symbol"):
            AIAnalysisService(WrongSymbol()).analyze(setup())


if __name__ == "__main__":
    unittest.main()
