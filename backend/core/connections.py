"""Redacted external-service configuration and deliberately local-only checks.

Every value here comes from the injected configuration object. There are no
environment fallbacks, no "MOCK means configured" branches, and no check that
reports PASSED for something that is not actually connected.
"""
from datetime import datetime

from backend.market_data.session import market_session


class ConnectionCenter:
    SECTIONS = {"market_data", "trading_platform", "ai", "notifications", "tradingview"}

    def __init__(self, *, config, market_data, universe, execution, performance, portfolio,
                 risk, notifier, owner_controls_enabled, clock, repository=None,
                 market_data_availability=None, tradingview_status=None,
                 tradingview_check=None):
        self.config = config
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
        self._availability_source = market_data_availability
        self._tradingview_status = tradingview_status
        self._tradingview_check = tradingview_check
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
        market = self.config.market_data.describe()
        broker = self.config.broker.describe()
        ai = self.config.ai.describe()
        notifications = self.config.notifications.describe()
        provider_state = self.market_data.status
        return {
            "market_data": {
                "provider": market["provider"], "label": market["label"],
                "mode": market["state"],
                "known": market["known"], "implemented": market["implemented"],
                "configured": market["configured"], "available": market["available"],
                "state": market["state"], "reason": market["reason"],
                "missing_credentials": market["missing_credentials"],
                "base_url_configured": market["base_url_configured"],
                "plan_configured": market["plan_configured"],
                "account_id_configured": market["account_id_configured"],
                "capabilities": provider_state.get("capabilities") or market["capabilities"],
                "live_provider": provider_state,
                "connectivity": {
                    "state": provider_state.get("state"),
                    "reason": provider_state.get("reason"),
                    "checked_at": provider_state.get("checked_at"),
                    "last_success_at": provider_state.get("last_success_at"),
                    "last_error_at": provider_state.get("last_error_at"),
                    "last_error": provider_state.get("last_error"),
                    "rate_limited_until": provider_state.get("rate_limited_until"),
                },
                "connection": self.last_tests.get("market_data", _never_tested()),
                "last_successful_connection": self.last_successful.get("market_data"),
            },
            "trading_platform": {
                "name": broker["label"] if broker["known"] else "Not connected",
                "label": broker["label"],
                "adapter": broker["provider"], "environment": broker["environment"],
                "known": broker["known"], "implemented": broker["implemented"],
                "configured": broker["configured"], "available": broker["available"],
                "state": broker["state"], "reason": broker["reason"],
                "missing_credentials": broker["missing_credentials"],
                "base_url_configured": broker["base_url_configured"],
                "account_id_configured": broker["account_id_configured"],
                "capabilities": self.execution.capabilities(),
                "account_connection": self.last_tests.get("trading_platform", _never_tested()),
                "last_successful_connection": self.last_successful.get("trading_platform"),
                "live_execution_available": False,
            },
            "paper_account": {
                "execution": self.execution.identify(),
                "mode": "SIMULATED",
                "market_data_authority": "NONE",
                "description": (
                    "Paper mode simulates cash, fills, positions, and P&L only. "
                    "Prices always come from a connected market-data provider or a "
                    "TradingView signal; this account never generates a price."),
            },
            "ai": {
                "provider": ai["provider"], "label": ai["label"], "model": ai["model"],
                "known": ai["known"], "implemented": ai["implemented"],
                "configured": ai["configured"], "available": ai.get("available"),
                "active": ai["active"], "state": ai["state"], "reason": ai["reason"],
                "missing_credentials": ai["missing_credentials"],
                "endpoint_configured": ai["endpoint_configured"],
                "enabled": ai["enabled"], "role": ai["role"], "authority": ai["authority"],
                "connection": self.last_tests.get("ai", _never_tested()),
                "last_successful_connection": self.last_successful.get("ai"),
            },
            "notifications": {
                "provider": notifications["provider"], "label": notifications["label"],
                "enabled": notifications["enabled"],
                "known": notifications["known"], "implemented": notifications["implemented"],
                "configured": notifications["configured"],
                "active_channels": notifications["active_channels"],
                "destination_configured": any(
                    channel["destination_configured"] for channel in notifications["channels"]),
                "connection": self.last_tests.get("notifications", _never_tested()),
                "last_successful_connection": self.last_successful.get("notifications"),
            },
            "tradingview": self._tradingview_section(),
        }

    def _tradingview_section(self) -> dict:
        """Configuration plus live webhook state, never a credential value.

        The webhook reports ``CONNECTED`` only after an authenticated signal
        has actually been processed; a configured secret on its own reports
        ``UNAVAILABLE`` (installed, no delivery observed yet) or
        ``NOT_CONFIGURED``.
        """
        settings = self.config.tradingview.describe()
        live = self._tradingview_status() if self._tradingview_status is not None else {}
        section = {**settings, **live}
        section.setdefault("state", "NOT_CONFIGURED")
        section.setdefault("reason", None)
        section.setdefault("endpoint_status", "NOT_INSTALLED")
        section.setdefault("webhook_available", False)
        section.setdefault("last_received_signal", None)
        section.setdefault("signals_accepted", 0)
        section.setdefault("signals_rejected", 0)
        section.setdefault("signals_duplicate", 0)
        section["connection"] = self.last_tests.get("tradingview", _never_tested())
        section["last_successful_connection"] = self.last_successful.get("tradingview")
        return section

    def market_data_availability(self) -> dict:
        """Five-state availability from the market-data bridge, honest about nothing."""
        if self._availability_source is None:
            return {"state": "NOT_CONFIGURED", "reason": "no market-data bridge is attached",
                    "last_quote_at": None, "stale_after_seconds": None, "refreshed_at": self.clock()}
        return self._availability_source()

    def test(self, section: str) -> dict:
        if section not in self.SECTIONS:
            raise ValueError("unsupported connection section")
        tested_at = self.clock()
        market = self.config.market_data
        broker = self.config.broker
        if section == "market_data":
            if not market.configured or market.provider in ("", "none"):
                detail = market.unavailable_reason or "no market data provider is connected"
                result = _not_configured(tested_at, f"{detail}; no network request was made")
            elif not market.available:
                detail = market.unavailable_reason or (
                    f"the {market.provider} market data adapter is unavailable")
                result = _unsupported(tested_at, f"{detail}; no network request was made")
            else:
                outcome = self.market_data.check()
                message = str(outcome.get("detail") or "")
                result = {
                    "status": str(outcome.get("status") or "FAILED"),
                    "success": bool(outcome.get("success")),
                    "tested_at": tested_at,
                    "error": None if outcome.get("success") else message,
                    "message": message,
                }
        elif section == "trading_platform":
            result = self._external_probe(
                label="trading platform", provider=broker.provider, configured=broker.configured,
                available=broker.available, reason=broker.unavailable_reason, tested_at=tested_at)
            if result["status"] == "PASSED":
                result["error"] = "No external broker adapter is installed; no network request was made"
                result["success"] = False
                result["status"] = "UNSUPPORTED"
        elif section == "ai":
            if self.config.ai.active:
                result = _test_result(True, tested_at,
                                      "Local advisory provider is available; it has no execution authority")
            elif self.config.ai.provider == "none" or not self.config.ai.enabled:
                result = _not_configured(tested_at, self.config.ai.reason or "AI analysis is not enabled")
            else:
                result = _unsupported(tested_at, f"the {self.config.ai.provider} AI adapter is not installed; "
                                                 "no network request was made")
        elif section == "notifications":
            channel = self.config.notifications
            if channel.active_channels:
                names = ", ".join(item.provider for item in channel.active_channels)
                result = _test_result(True, tested_at,
                                      f"Local notification sink is active ({names}); no external message was sent")
            else:
                result = _not_configured(tested_at, "no notification channel is configured")
        elif section == "tradingview":
            tv = self.config.tradingview
            if not tv.enabled or not tv.configured:
                result = _not_configured(tested_at, "TradingView webhook is not configured; "
                                                    "set TRADINGVIEW_ENABLED and TRADINGVIEW_WEBHOOK_SECRET")
            elif self._tradingview_check is None:
                result = _unsupported(tested_at, "the TradingView webhook endpoint is not installed; "
                                                 "no endpoint was opened")
            else:
                passed, detail = self._tradingview_check()
                result = _test_result(passed, tested_at, detail)
        else:
            result = _unsupported(tested_at, "No external adapter is installed; no network request was made")
        self.last_tests[section] = result
        if result.get("success"):
            self.last_successful[section] = tested_at
        result["last_successful_at"] = self.last_successful.get(section)
        return result

    @staticmethod
    def _external_probe(*, label, provider, configured, available, reason, tested_at) -> dict:
        detail = reason or f"no {label} provider is connected"
        if not configured or provider in ("", "none"):
            return _not_configured(tested_at, f"{detail}; no network request was made")
        if not available:
            return _unsupported(tested_at, f"{detail}; no network request was made")
        return _unsupported(tested_at, f"the {provider} {label} adapter is installed but has no probe yet; "
                                       "no network request was made")

    def readiness(self) -> dict:
        configured = self.status()
        availability = self.market_data_availability()
        performance = self.performance.calculate(
            as_of=market_session(self.clock()).trading_date, portfolio=self.portfolio.snapshot())
        data_test = self.last_tests.get("market_data", {}).get("success", False)
        broker_test = self.last_tests.get("trading_platform", {}).get("success", False)
        market_ready = configured["market_data"]["available"]
        broker_ready = configured["trading_platform"]["available"]
        checks = [
            _check("market_data_configured", "External market data configured", market_ready),
            _check("market_data_test_passed", "Market data connection tested", market_ready and data_test),
            _check("broker_configured", "Platform credentials configured", broker_ready),
            _check("broker_connection_tested", "Platform connection tested", broker_ready and broker_test),
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
        not_configured = [
            item["label"] for item in checks if not item["passed"]
            and item["key"] in ("market_data_configured", "market_data_test_passed",
                                "broker_configured", "broker_connection_tested")
        ]
        return {"status": "LOCKED", "live_mode": "LOCKED", "checks": checks,
                "market_data_state": configured["market_data"]["state"],
                "market_data_reason": configured["market_data"]["reason"],
                "market_data_availability": availability["state"],
                "market_data_availability_reason": availability["reason"],
                "market_data_last_quote_at": availability.get("last_quote_at"),
                "broker_state": configured["trading_platform"]["state"],
                "broker_reason": configured["trading_platform"]["reason"],
                "not_configured": not_configured,
                "performance_qualification": performance.qualification_status,
                "measured_win_rate": performance.evaluation_win_rate,
                "evaluation_trades": performance.evaluation_trades,
                "paper_mode": "ACTIVE", "blockers": [item["label"] for item in checks if not item["passed"]]}

    def _max_entry(self) -> float:
        return self.config.trading.max_candidate_price


def _never_tested() -> dict:
    return {"status": "NOT_TESTED", "success": False, "tested_at": None,
            "last_successful_at": None, "error": None}


def _test_result(success: bool, tested_at: datetime, message: str) -> dict:
    return {"status": "PASSED" if success else "FAILED", "success": success,
            "tested_at": tested_at, "error": None if success else message, "message": message}


def _not_configured(tested_at: datetime, reason: str) -> dict:
    return {"status": "NOT_CONFIGURED", "success": False, "tested_at": tested_at,
            "error": reason, "message": reason}


def _unsupported(tested_at: datetime, reason: str) -> dict:
    return {"status": "UNSUPPORTED", "success": False, "tested_at": tested_at,
            "error": reason, "message": reason}


def _check(key: str, label: str, passed: bool) -> dict:
    return {"key": key, "label": label, "status": "READY" if passed else "NOT_READY",
            "passed": passed, "detail": "Satisfied" if passed else "Not satisfied"}
