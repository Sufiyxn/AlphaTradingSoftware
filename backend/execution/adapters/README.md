# Adapter contract

Implement `TradingPlatformAdapter` with normalized values from
`backend.execution.models` and report supported behavior through
`PlatformCapabilities`. Adapters must not be imported by strategy, scanner,
risk, portfolio, or orchestrator modules. Register an adapter by name and
select it through `ExecutionRouter`.

The current mock adapter is local and deterministic. No external platform is
implemented or implied to be supported.
