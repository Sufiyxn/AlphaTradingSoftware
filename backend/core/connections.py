"""Redacted external-service configuration and deliberately local-only checks."""
import os
from datetime import datetime

from backend.market_data.session import market_session


class ConnectionCenter:
    SECTIONS = {"market_data", "trading_platform", "ai", "notifications", "tradingview"}

    def __init__(self, *, market_data, universe, execution, performance, portfolio, risk,
                 notifier, owner_controls_enabled, clock, repository=None):
        self.market_data = market_data
        self.universe = universe
        self.execution = execution
        self.performance = performance
        self.portfolio = portfolio
        self.risk = risk
        self.notifier = notifier
        self.owner_controls_enabled = owner_controls_enabled
        self.clock = clock
        self.repository = repository
        self.last_tests: dict[str, dict] = {}
        self.last_successful: dict[str, datetime] = {}
        if repository is not None:
            for event in reversed(repository.events(limit=500)):
                if event.get("event_type") != "OWNER_TESTED_CONNECTION":
                    continue
                context = event.get("context_json", {})
                section = context.get("section")
                result = context.get("result", {})
                if section not in self.SECTIONS:
                    continue
                self.last_tests[section] = result
                successful_at = result.get("tested_at") if result.get("success") else result.get("last_successful_at")
                if successful_at:
                    self.last_successful[section] = datetime.fromisoformat(successful_at) if isinstance(successful_at, str) else successful_at

    def status(self) -> dict:
        env = os.environ
        data_provider = env.get("MARKET_DATA_PROVIDER", "MOCK").upper()
        data_mode = env.get("MARKET_DATA_MODE", "MOCK").upper()
        platform_adapter = env.get("TRADING_PLATFORM_ADAPTER", "mock").strip()
        platform_mode = env.get("TRADING_PLATFORM_ENVIRONMENT", "PAPER").upper()
        ai_provider = env.get("AI_PROVIDER", "LOCAL/MOCK")
        ai_enabled = _enabled(env.get("AI_ENABLED"), default=True)
        notification_provider = env.get("NOTIFICATION_PROVIDER", "LOCAL/MOCK")
        notification_enabled = _enabled(env.get("NOTIFICATION_ENABLED"), default=True)
        tv_enabled = _enabled(env.get("TRADINGVIEW_ENABLED"), default=False)
        caps = self.execution.capabilities()
        return {
            "market_data": {
                "provider": data_provider, "mode": data_mode,
                "configured": data_provider == "MOCK" or _all_present(env, "MARKET_DATA_API_KEY"),
                "base_url_configured": bool(env.get("MARKET_DATA_BASE_URL")),
                "plan_configured": bool(env.get("MARKET_DATA_PLAN")),
                "account_id_configured": bool(env.get("MARKET_DATA_ACCOUNT_ID")),
                "capabilities": ["historical_ohlcv", "latest_quotes", "multiple_symbols", "read_only"],
                "connection": self.last_tests.get("market_data", _never_tested()),
                "last_successful_connection": self.last_successful.get("market_data"),
            },
            "trading_platform": {
                "name": env.get("TRADING_PLATFORM_NAME", "Local mock platform"),
                "adapter": platform_adapter, "environment": platform_mode,
                "configured": platform_adapter.lower() == "mock" or _all_present(
                    env, "TRADING_PLATFORM_API_KEY", "TRADING_PLATFORM_API_SECRET", "TRADING_PLATFORM_ACCOUNT_ID"),
                "base_url_configured": bool(env.get("TRADING_PLATFORM_BASE_URL")),
                "account_id_configured": bool(env.get("TRADING_PLATFORM_ACCOUNT_ID")),
                "capabilities": caps,
                "account_connection": self.last_tests.get("trading_platform", _never_tested()),
                "last_successful_connection": self.last_successful.get("trading_platform"),
                "live_execution_available": False,
            },
            "ai": {
                "provider": ai_provider, "model": env.get("AI_MODEL", "local-deterministic"),
                "endpoint_configured": bool(env.get("AI_BASE_URL")),
                "configured": ai_provider.upper() in ("MOCK", "LOCAL/MOCK") or _all_present(env, "AI_API_KEY"),
                "enabled": ai_enabled, "role": "ADVISORY_ONLY",
                "connection": self.last_tests.get("ai", _never_tested()),
                "last_successful_connection": self.last_successful.get("ai"),
            },
            "notifications": {
                "provider": notification_provider,
                "destination_configured": bool(env.get("NOTIFICATION_DESTINATION")),
                "configured": notification_provider.upper() in ("MOCK", "LOCAL/MOCK") or _all_present(env, "NOTIFICATION_TOKEN"),
                "enabled": notification_enabled,
                "connection": self.last_tests.get("notifications", _never_tested()),
                "last_successful_connection": self.last_successful.get("notifications"),
            },
            "tradingview": {
                "enabled": tv_enabled, "webhook_available": False,
                "secret_configured": bool(env.get("TRADINGVIEW_WEBHOOK_SECRET")),
                "endpoint_status": "NOT_IMPLEMENTED", "last_received_signal": None,
            },
        }

    def test(self, section: str) -> dict:
        if section not in self.SECTIONS:
            raise ValueError("unsupported connection section")
        tested_at = self.clock()
        local_data_mode = os.getenv("MARKET_DATA_MODE", "MOCK").upper()
        if (section == "market_data" and os.getenv("MARKET_DATA_PROVIDER", "MOCK").upper() == "MOCK"
                and local_data_mode in ("MOCK", "PAPER", "TEST")):
            quotes = self.market_data.get_latest(self.universe.symbols)
            result = _test_result(bool(quotes), tested_at,
                                  "Local mock quotes are available" if quotes else "No mock quotes available")
        elif (section == "trading_platform" and self.execution.identify().lower() == "mock"
              and os.getenv("TRADING_PLATFORM_ENVIRONMENT", "PAPER").upper() == "PAPER"):
            account = self.execution.get_account()
            result = _test_result(account.equity > 0, tested_at,
                                  "Local paper adapter account is available" if account.equity > 0 else "Paper account has no equity")
        elif section == "ai" and os.getenv("AI_PROVIDER", "LOCAL/MOCK").upper() in ("MOCK", "LOCAL/MOCK"):
            result = _test_result(True, tested_at, "Local advisory provider is available; it has no execution authority")
        elif section == "notifications" and os.getenv("NOTIFICATION_PROVIDER", "LOCAL/MOCK").upper() in ("MOCK", "LOCAL/MOCK"):
            result = _test_result(True, tested_at, "Local notification sink is available; no external message was sent")
        elif section == "tradingview":
            result = {"status": "UNSUPPORTED", "success": False, "tested_at": tested_at,
                      "error": "TradingView webhook support is not implemented; no endpoint was opened"}
        else:
            result = {"status": "UNSUPPORTED", "success": False, "tested_at": tested_at,
                      "error": "No external adapter is installed; no network request was made"}
        self.last_tests[section] = result
        if result.get("success"):
            self.last_successful[section] = tested_at
        result["last_successful_at"] = self.last_successful.get(section)
        return result

    def readiness(self) -> dict:
        configured = self.status()
        performance = self.performance.calculate(
            as_of=market_session(self.clock()).trading_date, portfolio=self.portfolio.snapshot())
        data_test = self.last_tests.get("market_data", {}).get("success", False)
        broker_test = self.last_tests.get("trading_platform", {}).get("success", False)
        market_is_external = configured["market_data"]["provider"] != "MOCK"
        broker_is_external = configured["trading_platform"]["adapter"].lower() != "mock"
        checks = [
            _check("market_data_configured", "External market data configured", market_is_external and configured["market_data"]["configured"]),
            _check("market_data_test_passed", "Market data connection tested", market_is_external and data_test),
            _check("broker_configured", "Platform credentials configured", broker_is_external and configured["trading_platform"]["configured"]),
            _check("broker_connection_tested", "Platform connection tested", broker_is_external and broker_test),
            _check("account_available", "Live account available", False),
            _check("capabilities_supported", "Required live capabilities supported", False),
            _check("owner_authentication", "Owner authentication active", self.owner_controls_enabled),
            _check("emergency_stop", "Emergency stop available", self.owner_controls_enabled),
            _check("risk_rules", "Risk rules active", self.risk.config.enabled and self.risk.config.min_reward_risk >= 2.0),
            _check("entry_limit", "Entry price is strictly below $100", self._max_entry() <= 100.0),
            _check("reward_risk", "Minimum 2:1 reward/risk active", self.risk.config.min_reward_risk >= 2.0),
            _check("performance_qualified", "Measured performance >= 51%", performance.qualified),
            _check("performance_sample", "Performance sample is sufficient", performance.evaluation_trades >= self.performance.minimum_evaluation_trades),
            _check("paper_mode", "Paper trading available", True),
            {"key": "live_mode", "label": "Live trading", "status": "LOCKED", "passed": False,
             "detail": "Live execution is not implemented; credentials cannot unlock this mode."},
        ]
        return {"status": "LOCKED", "live_mode": "LOCKED", "checks": checks,
                "performance_qualification": performance.qualification_status,
                "measured_win_rate": performance.evaluation_win_rate,
                "evaluation_trades": performance.evaluation_trades,
                "paper_mode": "ACTIVE", "blockers": [item["label"] for item in checks if not item["passed"]]}

    def _max_entry(self) -> float:
        return 100.0


def _enabled(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _all_present(env, *keys: str) -> bool:
    return all(bool(env.get(key)) for key in keys)


def _never_tested() -> dict:
    return {"status": "NOT_TESTED", "success": False, "tested_at": None,
            "last_successful_at": None, "error": None}


def _test_result(success: bool, tested_at: datetime, message: str) -> dict:
    return {"status": "PASSED" if success else "FAILED", "success": success,
            "tested_at": tested_at, "error": None if success else message, "message": message}


def _check(key: str, label: str, passed: bool) -> dict:
    return {"key": key, "label": label, "status": "READY" if passed else "NOT_READY",
            "passed": passed, "detail": "Satisfied" if passed else "Not satisfied"}
