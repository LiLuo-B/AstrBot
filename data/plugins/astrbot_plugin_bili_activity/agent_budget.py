"""Runner-local accounting for AstrBot 4.29 Provider invocations.

This measures Provider calls, including fallback and empty-output retries, not
every HTTP request hidden inside a Provider. Request-level retries are limited
through the native runner option. Memory hooks and embeddings are outside this
adapter; media calls are charged explicitly by the activity plugin.
"""
from __future__ import annotations


class BudgetGuard:
    """Retain budget failures even when native fallback catches exceptions."""

    def __init__(self, runner, charge):
        self.runner, self.charge = runner, charge
        self.error = None

    def before_call(self):
        self.raise_if_blocked()
        try:
            self.charge()
        except Exception as exc:
            self.error = exc
            # AstrBot checks this before fallback or the next tool/model step.
            # Every proxy also remembers the failure, so it cannot be retried
            # for free even if a future runner does not honor request_stop.
            self.runner.request_stop()
            raise

    def raise_if_blocked(self):
        if self.error is not None:
            raise self.error


class _BudgetedProvider:
    """Delegate capabilities without modifying the shared front-chat provider."""

    def __init__(self, provider, guard):
        self._provider, self._guard = provider, guard

    def __getattr__(self, name):
        return getattr(self._provider, name)

    async def text_chat(self, *args, **kwargs):
        self._guard.before_call()
        return await self._provider.text_chat(*args, **kwargs)

    async def text_chat_stream(self, *args, **kwargs):
        self._guard.before_call()
        stream = self._provider.text_chat_stream(*args, **kwargs)
        try:
            async for response in stream:
                yield response
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()


def instrument_runner(runner, charge):
    """Install accounting after reset and before step_until_done.

    Args:
        runner: A reset AstrBot 4.29 ToolLoopAgentRunner.
        charge: Synchronous callable that reserves one model call or raises.

    Returns:
        A guard whose raise_if_blocked() must be called after the runner loop.

    Raises:
        RuntimeError: The runner is incompatible, already wrapped, or uses an
            LLM/custom compressor whose calls would escape accounting.
    """
    if not callable(charge) or not callable(getattr(runner, "request_stop", None)):
        raise RuntimeError("unsupported native Agent budget interface")
    if getattr(runner, "_dafeiyu_budget_guard", None) is not None:
        raise RuntimeError("native Agent budget is already installed")
    config = getattr(runner, "request_context_manager_config", None)
    for component in (runner, config):
        if component is not None and (
            getattr(component, "llm_compress_provider", None) is not None
            or getattr(component, "custom_compressor", None) is not None
        ):
            raise RuntimeError("budgeted activities require turn truncation, not model compression")
    provider = getattr(runner, "provider", None)
    fallbacks = getattr(runner, "fallback_providers", [])
    if not isinstance(fallbacks, (list, tuple)):
        raise RuntimeError("unsupported fallback provider layout")
    providers = [provider, *fallbacks]
    for item in providers:
        if (not callable(getattr(item, "text_chat", None))
                or not isinstance(getattr(item, "provider_config", None), dict)
                or (getattr(runner, "streaming", False)
                    and not callable(getattr(item, "text_chat_stream", None)))):
            raise RuntimeError("unsupported native Provider budget interface")
    guard = BudgetGuard(runner, charge)
    wrappers = {id(item): _BudgetedProvider(item, guard) for item in providers}
    runner.provider = wrappers[id(provider)]
    runner.fallback_providers = [wrappers[id(item)] for item in fallbacks]
    # AstrBot 4.29 passes this option through its Provider contract. OpenAI's
    # implementation treats it as max_attempts, so one disables hidden retries.
    # A different Provider may ignore it; counters still describe invocations.
    runner.request_max_retries = 1
    runner._dafeiyu_budget_guard = guard
    return guard
