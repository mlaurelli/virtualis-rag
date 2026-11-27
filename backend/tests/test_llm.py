"""Test del client Kimi con endpoint Moonshot mockato via respx (nessuna rete reale)."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
import respx

from app import llm as llm_module
from app.config import Settings, get_settings
from app.llm import (
    KimiClient,
    LLMInsufficientBalanceError,
    LLMNotConfiguredError,
    extract_json,
)

BASE = "https://api.moonshot.ai/v1"


def _settings() -> Settings:
    return Settings(
        moonshot_api_key="sk-test",
        kimi_model="kimi-k3",
        kimi_reasoning_effort="high",
        _env_file=None,  # type: ignore[call-arg]
    )


def _client() -> KimiClient:
    return KimiClient(_settings())


def _completion_payload(text: str) -> dict:
    """Corpo di risposta OpenAI-compatible per una chat completion."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@respx.mock
async def test_chat_restituisce_testo() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion_payload("risposta di prova"))
    )
    out = await _client().chat([{"role": "user", "content": "ciao"}])
    assert out == "risposta di prova"
    request = route.calls[0].request
    body = json.loads(request.content)
    assert body["model"] == "kimi-k3"
    assert body["stream"] is False
    # reasoning_effort passato come extra_body
    assert body["reasoning_effort"] == "high"


@respx.mock
async def test_chat_json_mode_passa_response_format() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion_payload('{"ok": true}'))
    )
    out = await _client().chat(
        [{"role": "user", "content": "dammi un json"}], json_mode=True
    )
    assert out == '{"ok": true}'
    body = json.loads(route.calls[0].request.content)
    assert body["response_format"] == {"type": "json_object"}


@respx.mock
async def test_chat_stream_concatena_delta() -> None:
    # Stream SSE con tre delta di contenuto
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "Ciao "}}]},
        {"choices": [{"index": 0, "delta": {"content": "mondo"}}]},
        {"choices": [{"index": 0, "delta": {"content": "!"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    sse_body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )
    )
    parts = [p async for p in _client().chat_stream([{"role": "user", "content": "ciao"}])]
    assert "".join(parts) == "Ciao mondo!"
    body = json.loads(route.calls[0].request.content)
    assert body["stream"] is True


def test_extract_json_semplice() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_con_code_fence() -> None:
    text = 'Ecco il risultato:\n```json\n{"a": 1, "b": [2, 3]}\n```\nFine.'
    assert extract_json(text) == {"a": 1, "b": [2, 3]}


def test_extract_json_con_testo_extra() -> None:
    assert extract_json('prefisso {"x": {"y": 2}} suffisso') == {"x": {"y": 2}}


def test_extract_json_senza_json() -> None:
    with pytest.raises(ValueError):
        extract_json("nessun json qui")


def test_llm_non_configurato() -> None:
    s = Settings(moonshot_api_key="", _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(LLMNotConfiguredError):
        KimiClient(s)


# --- resilienza: retry su errori transitori ------------------------------------


def _settings_resilient(**override: Any) -> Settings:
    """Settings di test con retry veloci (base 1ms) e 4 tentativi."""
    params: dict[str, Any] = {
        "moonshot_api_key": "sk-test",
        "kimi_model": "kimi-k3",
        "kimi_reasoning_effort": "high",
        "llm_retry_base_seconds": 0.001,
        "llm_max_retries": 4,
        "_env_file": None,
    }
    params.update(override)
    return Settings(**params)  # type: ignore[call-arg]


@pytest.fixture
def sleep_spy(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Sostituisce asyncio.sleep con una spia che registra le attese."""
    import asyncio

    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return waits


def _error_body(message: str) -> dict:
    return {"error": {"message": message, "type": "test_error"}}


@respx.mock
async def test_chat_429_poi_successo_con_un_retry(sleep_spy: list[float]) -> None:
    """Sequenza 429→200: la chiamata riesce dopo esattamente un'attesa."""
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(429, json=_error_body("rate limit raggiunto")),
            httpx.Response(200, json=_completion_payload("ok dopo retry")),
        ]
    )
    out = await KimiClient(_settings_resilient()).chat(
        [{"role": "user", "content": "ciao"}]
    )
    assert out == "ok dopo retry"
    assert len(route.calls) == 2
    assert len(sleep_spy) == 1
    assert sleep_spy[0] > 0


@respx.mock
async def test_chat_retry_rispetta_retry_after(sleep_spy: list[float]) -> None:
    """L'header Retry-After del 429 fissa l'attesa minima prima del retry."""
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(
                429,
                json=_error_body("rate limit raggiunto"),
                headers={"retry-after": "2.5"},
            ),
            httpx.Response(200, json=_completion_payload("ok")),
        ]
    )
    out = await KimiClient(_settings_resilient()).chat(
        [{"role": "user", "content": "ciao"}]
    )
    assert out == "ok"
    assert len(route.calls) == 2
    assert len(sleep_spy) == 1
    assert sleep_spy[0] >= 2.5


@respx.mock
async def test_chat_401_nessun_retry(sleep_spy: list[float]) -> None:
    """401 è un errore permanente: fallisce subito, senza attese né retry."""
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(401, json=_error_body("chiave non valida"))
    )
    with pytest.raises(openai.AuthenticationError):
        await KimiClient(_settings_resilient()).chat(
            [{"role": "user", "content": "ciao"}]
        )
    assert len(route.calls) == 1
    assert sleep_spy == []


@respx.mock
async def test_chat_saldo_esaurito_nessun_retry(sleep_spy: list[float]) -> None:
    """429 da saldo esaurito/account sospeso: errore permanente e chiaro,
    riconosciuto dal messaggio, MAI ritentato."""
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            429, json=_error_body("account suspended: insufficient balance")
        )
    )
    with pytest.raises(LLMInsufficientBalanceError, match=r"[Ss]aldo esaurito"):
        await KimiClient(_settings_resilient()).chat(
            [{"role": "user", "content": "ciao"}]
        )
    assert len(route.calls) == 1
    assert sleep_spy == []


@respx.mock
async def test_chat_503_esaurisce_i_tentativi(sleep_spy: list[float]) -> None:
    """503 persistente: si ritenta fino a llm_max_retries, poi si propaga."""
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(503, json=_error_body("servizio non disponibile"))
    )
    with pytest.raises(openai.APIStatusError):
        await KimiClient(_settings_resilient(llm_max_retries=3)).chat(
            [{"role": "user", "content": "ciao"}]
        )
    assert len(route.calls) == 3
    assert len(sleep_spy) == 2
    # backoff esponenziale crescente (tolleranza per il jitter)
    assert sleep_spy[1] >= sleep_spy[0]


def _sse_body(*texts: str) -> str:
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": t}}]} for t in texts
    ] + [{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


@respx.mock
async def test_chat_stream_429_prima_dei_delta_ritenta(sleep_spy: list[float]) -> None:
    """Stream: errore PRIMA del primo delta → retry consentito."""
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(429, json=_error_body("rate limit raggiunto")),
            httpx.Response(
                200,
                content=_sse_body("Ciao ", "mondo"),
                headers={"content-type": "text/event-stream"},
            ),
        ]
    )
    parts = [
        p
        async for p in KimiClient(_settings_resilient()).chat_stream(
            [{"role": "user", "content": "ciao"}]
        )
    ]
    assert "".join(parts) == "Ciao mondo"
    assert len(route.calls) == 2
    assert len(sleep_spy) == 1


def _fake_chunk(text: str) -> Any:
    """Chunk di stream finto con la stessa forma di quelli del SDK."""
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
    )


def _fake_stream(texts: list[str], error: Exception | None = None) -> Any:
    """AsyncIterator finto: emette i delta poi, se dato, solleva l'errore."""

    async def _gen() -> Any:
        for text in texts:
            yield _fake_chunk(text)
        if error is not None:
            raise error

    return _gen()


def _conn_error() -> openai.APIConnectionError:
    request = httpx.Request("POST", f"{BASE}/chat/completions")
    return openai.APIConnectionError(message="connessione caduta", request=request)


async def test_chat_stream_errore_a_meta_nessun_retry(
    monkeypatch: pytest.MonkeyPatch, sleep_spy: list[float]
) -> None:
    """Stream interrotto DOPO il primo delta: ritentare ripeterebbe contenuto
    già emesso → l'eccezione è propagata senza alcun retry."""
    client = KimiClient(_settings_resilient())
    calls = 0

    async def fake_create(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return _fake_stream(["Ciao "], error=_conn_error())

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    parts: list[str] = []
    with pytest.raises(openai.APIConnectionError):
        async for token in client.chat_stream([{"role": "user", "content": "ciao"}]):
            parts.append(token)
    assert parts == ["Ciao "]  # il token già emesso non si perde
    assert calls == 1
    assert sleep_spy == []


async def test_chat_stream_errore_rete_prima_dei_delta_ritenta(
    monkeypatch: pytest.MonkeyPatch, sleep_spy: list[float]
) -> None:
    """Errore di rete alla creazione dello stream: retry e successo."""
    client = KimiClient(_settings_resilient())
    calls = 0

    async def fake_create(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _conn_error()
        return _fake_stream(["ripartito"])

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    parts = [p async for p in client.chat_stream([{"role": "user", "content": "ciao"}])]
    assert parts == ["ripartito"]
    assert calls == 2
    assert len(sleep_spy) == 1


@respx.mock
async def test_client_imposta_timeout_esplicito() -> None:
    """Il timeout per singola richiesta è impostato sul client httpx del SDK."""
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion_payload("ok"))
    )
    client = KimiClient(_settings_resilient(llm_request_timeout=42.0))
    assert client._client.timeout == 42.0
    await client.chat([{"role": "user", "content": "ciao"}])


# --- function calling (chat_with_tools) ------------------------------------------

_TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "hybrid_search",
            "description": "Ricerca sui documenti",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
]


def _tool_completion_payload(arguments: str) -> dict:
    """Risposta OpenAI-compatible con un tool_call."""
    return {
        "id": "chatcmpl-tools",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "hybrid_search",
                                "arguments": arguments,
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@respx.mock
async def test_chat_with_tools_parsing_e_parametri() -> None:
    """Tool_call decodificato (id, nome, argomenti dict); tools e tool_choice
    inviati nella richiesta."""
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_tool_completion_payload('{"query": "rimborso"}')
        )
    )
    content, calls = await _client().chat_with_tools(
        [{"role": "user", "content": "cerca il rimborso"}], _TOOLS_SPEC
    )
    assert content == ""
    assert calls == [
        {"id": "call_1", "name": "hybrid_search", "arguments": {"query": "rimborso"}}
    ]
    body = json.loads(route.calls[0].request.content)
    assert body["tools"] == _TOOLS_SPEC
    assert body["tool_choice"] == "auto"


@respx.mock
async def test_chat_with_tools_argomenti_malformati() -> None:
    """Argomenti non JSON → ``arguments=None`` (segnalato al chiamante)."""
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_tool_completion_payload("non-json{")
        )
    )
    _, calls = await _client().chat_with_tools(
        [{"role": "user", "content": "cerca"}], _TOOLS_SPEC
    )
    assert calls[0]["arguments"] is None


@respx.mock
async def test_chat_with_tools_retry_su_errore_transitorio(
    sleep_spy: list[float],
) -> None:
    """La resilienza di chat vale anche per il tool-calling: 429 → retry."""
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(429, json=_error_body("rate limit raggiunto")),
            httpx.Response(200, json=_tool_completion_payload('{"query": "q"}')),
        ]
    )
    _, calls = await KimiClient(_settings_resilient()).chat_with_tools(
        [{"role": "user", "content": "cerca"}], _TOOLS_SPEC
    )
    assert calls[0]["arguments"] == {"query": "q"}
    assert len(route.calls) == 2
    assert len(sleep_spy) == 1


# --- semaforo globale di concorrenza verso il provider ----------------------------


@pytest.fixture
def semaphore_env(monkeypatch):
    """Semaforo azzerato e dimensione fissata via env (ripristino a fine test)."""

    def configure(size: int) -> None:
        monkeypatch.setenv("LLM_MAX_CONCURRENCY", str(size))
        get_settings.cache_clear()
        llm_module.reset_llm_semaphore()

    yield configure
    llm_module.reset_llm_semaphore()
    get_settings.cache_clear()


async def test_semaforo_limita_le_chiamate_concorrenti(semaphore_env, monkeypatch) -> None:
    """Con llm_max_concurrency=2 non più di due richieste raggiungono il
    provider contemporaneamente, anche con molte chat in parallelo."""
    semaphore_env(2)
    client = _client()
    active = 0
    max_active = 0

    async def fake_create(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    results = await asyncio.gather(
        *(client.chat([{"role": "user", "content": "ciao"}]) for _ in range(6))
    )
    assert results == ["ok"] * 6
    assert max_active == 2  # mai oltre il tetto, ma il tetto è sfruttato


async def test_semaforo_tenuto_per_tutta_la_durata_dello_stream(
    semaphore_env, monkeypatch
) -> None:
    """Lo slot resta occupato finché lo stream non è esaurito: con
    concorrenza 1 una chat parte solo DOPO la fine dello stream in corso."""
    semaphore_env(1)
    client = _client()
    order: list[str] = []

    async def fake_create(**kwargs):
        if kwargs.get("stream"):

            async def gen():
                order.append("stream-start")
                for text in ("Ciao ", "mondo"):
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
                    )
                    await asyncio.sleep(0.02)
                order.append("stream-end")

            return gen()
        order.append("chat")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)

    async def consume_stream() -> str:
        parts = []
        async for token in client.chat_stream([{"role": "user", "content": "ciao"}]):
            parts.append(token)
        return "".join(parts)

    stream_task = asyncio.create_task(consume_stream())
    # Attende che lo stream sia partito (e tenga lo slot del semaforo)
    while "stream-start" not in order:
        await asyncio.sleep(0.005)
    chat_task = asyncio.create_task(client.chat([{"role": "user", "content": "ciao"}]))
    assert await stream_task == "Ciao mondo"
    assert await chat_task == "ok"
    # La chat è entrata solo dopo l'esaurimento dello stream
    assert order.index("chat") > order.index("stream-end")
