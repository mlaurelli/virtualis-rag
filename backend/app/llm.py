"""Wrapper async attorno al client OpenAI verso Kimi (Moonshot, OpenAI-compatible)."""

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any, TypedDict

import structlog
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from .config import Settings, get_settings

log = structlog.get_logger(__name__)

Message = dict[str, Any]


class ToolCall(TypedDict):
    """Una chiamata a strumento richiesta dal modello (function calling OpenAI).

    ``arguments`` è il dict degli argomenti già decodificato dal JSON del
    modello; ``None`` se il JSON non era parseabile (tool_call malformato).
    """

    id: str
    name: str
    arguments: dict[str, Any] | None

# Semaforo GLOBALE di concorrenza verso il provider LLM (dimensione da
# ``llm_max_concurrency``): ogni chiamata al modello — streaming incluso, per
# l'intera durata dello stream — lo attraversa. Con Ollama (richieste servite
# in serie) evita che job paralleli si accumulino sul provider. Nessun rischio
# di deadlock: l'agente fa chiamate sequenziali, mai annidate (nessun codice
# chiama il modello mentre consuma uno stream).
_llm_semaphore: asyncio.Semaphore | None = None


def _get_llm_semaphore() -> asyncio.Semaphore:
    """Semaforo condiviso, creato lazy alla prima chiamata."""
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(
            max(1, get_settings().llm_max_concurrency)
        )
    return _llm_semaphore


def reset_llm_semaphore() -> None:
    """Azzera il semaforo (usato dai test per rileggere le settings)."""
    global _llm_semaphore
    _llm_semaphore = None


# Status HTTP considerati transitori (ritentabili): rate limit e errori server
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
# Marcatori nel messaggio d'errore che indicano saldo esaurito/account
# sospeso: errore PERMANENTE anche se arriva con status 429, mai ritentare
_BALANCE_MARKERS = ("insufficient balance", "suspended")


class LLMNotConfiguredError(RuntimeError):
    """Sollevata quando moonshot_api_key non è impostata."""

    def __init__(self) -> None:
        super().__init__(
            "LLM non configurato: impostare MOONSHOT_API_KEY nell'ambiente o in .env"
        )


class LLMInsufficientBalanceError(RuntimeError):
    """Saldo esaurito o account sospeso sul provider LLM.

    Errore permanente: ritentare non serve, ricaricare il saldo o
    riattivare l'account. Riconosciuto dai marcatori nel messaggio del
    provider (arriva tipicamente con status 429, altrimenti ritentabile).
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            "Saldo esaurito o account sospeso sul provider LLM: ricaricare il "
            f"saldo o riattivare l'account e riprovare. Dettaglio: {detail[:300]}"
        )


class KimiClient:
    """Client minimale per il modello di generazione remoto Kimi."""

    def __init__(self, settings: Settings) -> None:
        if not settings.moonshot_api_key:
            raise LLMNotConfiguredError()
        self._settings = settings
        self._client = AsyncOpenAI(
            api_key=settings.moonshot_api_key,
            base_url=settings.kimi_base_url,
            # Timeout esplicito per singola richiesta e retry interni del SDK
            # disattivati: la politica di retry è gestita qui (backoff + jitter,
            # Retry-After, distinzione errori transitori/permanenti)
            timeout=settings.llm_request_timeout,
            max_retries=0,
        )

    def _build_kwargs(
        self,
        messages: list[Message],
        *,
        temperature: float,
        json_mode: bool,
        max_tokens: int | None,
        stream: bool,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Costruisce i parametri della chiamata chat.completions."""
        kwargs: dict[str, Any] = {
            "model": self._settings.kimi_model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        # ``reasoning_effort`` è specifico di Kimi: si invia solo se valorizzato
        # (vuoto = endpoint OpenAI-compatibili che non lo supportano, es. Ollama)
        if self._settings.kimi_reasoning_effort:
            kwargs["extra_body"] = {
                "reasoning_effort": self._settings.kimi_reasoning_effort
            }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            # Function calling OpenAI: il modello decide quale strumento
            # invocare ("auto"); gli schemi seguono il formato
            # {"type": "function", "function": {...}}
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    # --- resilienza: retry su errori transitori -----------------------------

    @staticmethod
    def _check_balance(exc: APIStatusError) -> None:
        """Solleva :class:`LLMInsufficientBalanceError` se l'errore è un saldo
        esaurito/account sospeso (permanente anche con status 429)."""
        detail = f"{exc.message} {exc.body or ''}".lower()
        if any(marker in detail for marker in _BALANCE_MARKERS):
            raise LLMInsufficientBalanceError(exc.message) from exc

    @staticmethod
    def _retry_after_seconds(exc: APIStatusError) -> float | None:
        """Valore dell'header Retry-After in secondi, se presente e valido."""
        response = getattr(exc, "response", None)
        if response is None:
            return None
        raw = response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None

    def _backoff_seconds(self, attempt: int, retry_after: float | None = None) -> float:
        """Attesa prima del prossimo tentativo: Retry-After se il server lo
        chiede, altrimenti backoff esponenziale con jitter pieno."""
        base = self._settings.llm_retry_base_seconds
        if retry_after is not None:
            # Rispettato l'header; un jitter minimo evita retry sincronizzati
            return retry_after + random.uniform(0, min(base, 1.0))
        return base * (2 ** (attempt - 1)) + random.uniform(0, base)

    def _handle_retryable_error(
        self, exc: Exception, *, operation: str, attempt: int
    ) -> float | None:
        """Decide se l'errore è ritentabile e con quale attesa.

        Restituisce i secondi da attendere prima del prossimo tentativo, o
        ``None`` se non restano tentativi / l'errore non è transitorio.
        Solleva :class:`LLMInsufficientBalanceError` per saldo esaurito.
        """
        attempts = max(1, self._settings.llm_max_retries)
        retry_after: float | None = None
        reason: str
        if isinstance(exc, APIStatusError):
            self._check_balance(exc)
            if exc.status_code not in _TRANSIENT_STATUSES:
                return None  # 400/401/403 e altri permanenti: fallire subito
            reason = f"http {exc.status_code}"
            retry_after = self._retry_after_seconds(exc)
        elif isinstance(exc, APIConnectionError):
            # Copre anche APITimeoutError (sottoclasse): rete e timeout
            reason = type(exc).__name__
        else:
            return None
        if attempt >= attempts:
            return None
        wait = self._backoff_seconds(attempt, retry_after)
        log.warning(
            "errore transitorio del LLM: nuovo tentativo",
            operation=operation,
            reason=reason,
            attempt=attempt,
            max_attempts=attempts,
            wait_seconds=round(wait, 2),
        )
        return wait

    async def chat(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        """Completamento non-streaming: restituisce il testo della risposta.

        Gli errori transitori (429, 5xx, timeout, rete) sono ritentati con
        backoff esponenziale + jitter fino a ``llm_max_retries`` tentativi;
        gli errori permanenti (400/401/403, saldo esaurito) falliscono subito.
        """
        kwargs = self._build_kwargs(
            messages,
            temperature=temperature,
            json_mode=json_mode,
            max_tokens=max_tokens,
            stream=False,
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                async with _get_llm_semaphore():
                    response = await self._client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                return content or ""
            except Exception as exc:
                wait = self._handle_retryable_error(exc, operation="chat", attempt=attempt)
                if wait is None:
                    raise
                await asyncio.sleep(wait)

    async def chat_with_tools(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> tuple[str, list[ToolCall]]:
        """Chat con function calling OpenAI: ``(contenuto, tool_calls)``.

        ``tools`` è la lista degli schemi degli strumenti nel formato
        ``{"type": "function", "function": {"name", "description",
        "parameters"}}``. Restituisce il testo della risposta (vuoto se il
        modello ha solo invocato strumenti) e le chiamate richieste, con gli
        argomenti già decodificati (``None`` se il JSON era malformato: il
        chiamante decide come trattare il caso). Stessa politica di retry di
        :meth:`chat` su errori transitori.
        """
        kwargs = self._build_kwargs(
            messages,
            temperature=temperature,
            json_mode=False,
            max_tokens=max_tokens,
            stream=False,
            tools=tools,
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                async with _get_llm_semaphore():
                    response = await self._client.chat.completions.create(**kwargs)
                message = response.choices[0].message
                content = message.content or ""
                tool_calls: list[ToolCall] = []
                for raw in message.tool_calls or []:
                    try:
                        parsed: Any = json.loads(raw.function.arguments or "{}")
                        arguments = parsed if isinstance(parsed, dict) else None
                    except (json.JSONDecodeError, TypeError):
                        arguments = None
                    tool_calls.append(
                        {
                            "id": raw.id or "",
                            "name": raw.function.name or "",
                            "arguments": arguments,
                        }
                    )
                return content, tool_calls
            except Exception as exc:
                wait = self._handle_retryable_error(
                    exc, operation="chat_with_tools", attempt=attempt
                )
                if wait is None:
                    raise
                await asyncio.sleep(wait)

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Completamento in streaming: yield dei delta di testo.

        Il retry è possibile solo PRIMA del primo delta: se lo stream si
        interrompe dopo il primo token ritentare ripeterebbe contenuto già
        emesso, quindi l'eccezione è propagata al chiamante.
        """
        kwargs = self._build_kwargs(
            messages,
            temperature=temperature,
            json_mode=False,
            max_tokens=max_tokens,
            stream=True,
        )
        attempt = 0
        first_token_seen = False
        while True:
            attempt += 1
            try:
                # Il semaforo è tenuto per l'INTERA durata dello stream: verso
                # un provider seriale (Ollama) lo slot è occupato finché il
                # modello genera, non solo all'apertura della richiesta
                async with _get_llm_semaphore():
                    stream = await self._client.chat.completions.create(**kwargs)
                    async for chunk in stream:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        if delta and delta.content:
                            first_token_seen = True
                            yield delta.content
                return
            except Exception as exc:
                if first_token_seen:
                    raise  # stream già iniziato: nessun retry possibile
                wait = self._handle_retryable_error(
                    exc, operation="chat_stream", attempt=attempt
                )
                if wait is None:
                    raise
                await asyncio.sleep(wait)


def extract_json(text: str) -> dict[str, Any]:
    """Estrae un oggetto JSON da un testo, tollerando code fence e testo extra."""
    candidate = text.strip()
    # Rimuovi eventuale code fence ```json ... ```
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, flags=re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Fallback: primo blocco { ... } bilanciato per parentesi graffe
        start = candidate.find("{")
        if start == -1:
            raise ValueError(f"Nessun JSON trovato nel testo: {text[:200]!r}") from None
        depth = 0
        for i in range(start, len(candidate)):
            if candidate[i] == "{":
                depth += 1
            elif candidate[i] == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(candidate[start : i + 1])
        raise ValueError(f"JSON non bilanciato nel testo: {text[:200]!r}") from None


@lru_cache
def get_kimi_client() -> KimiClient:
    """Singleton del client Kimi (solleva LLMNotConfiguredError se senza chiave)."""
    return KimiClient(get_settings())
