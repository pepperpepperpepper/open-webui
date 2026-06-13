"""Serper web-search tool server for Open WebUI (OpenAPI tool server).

Open WebUI connects to this as an external OpenAPI "tool server". When a model
runs with native function calling, the `web_search` operation below is exposed
to the model as a callable tool. Open WebUI fetches the spec from
`/openapi.json` and executes calls by matching `operationId` -> route, so the
operationId (`web_search`) is the function name the model sees.

This exists because Open WebUI's *builtin* `search_web` routes Serper results
through the RAG embed/retrieve pipeline, which dilutes result quality for a
voice/chat assistant that just wants clean snippets. This server hands the raw
top organic results (plus answer box / knowledge graph when present) straight
back to the model.

Env:
  SERPER_API_KEY    - serper.dev API key (required; without it /search 503s)
  SERPER_TOOL_TOKEN - bearer token required on /search (optional; if unset, no auth)
  PORT              - listen port (default 8093); always binds 127.0.0.1
"""

import os

import aiohttp
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()
SERPER_TOOL_TOKEN = os.getenv("SERPER_TOOL_TOKEN", "").strip()
SERPER_ENDPOINT = "https://google.serper.dev/search"

app = FastAPI(
    title="Serper Web Search",
    description="Google web search (via Serper) exposed as an Open WebUI tool server.",
    version="1.0.0",
)


class SearchRequest(BaseModel):
    query: str = Field(
        ...,
        description="The search query — a natural-language question or keywords.",
        examples=["current bitcoin price USD"],
    )
    num: int = Field(
        6,
        ge=1,
        le=10,
        description="Maximum number of organic results to return (1-10).",
    )


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str


class SearchResponse(BaseModel):
    query: str
    answer: str | None = Field(
        default=None,
        description="A direct answer (from Serper's answer box / knowledge graph), if available.",
    )
    results: list[SearchResult]


def _require_auth(authorization: str | None) -> None:
    if not SERPER_TOOL_TOKEN:
        return
    expected = f"Bearer {SERPER_TOOL_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health", include_in_schema=False)
async def health():
    return {"ok": True, "configured": bool(SERPER_API_KEY)}


@app.post(
    "/search",
    operation_id="web_search",
    summary="Search the web",
    response_model=SearchResponse,
)
async def web_search(
    body: SearchRequest,
    authorization: str | None = Header(default=None),
) -> SearchResponse:
    """Search the web with Google (via Serper) for current, real-time, or
    factual information.

    Use this whenever the user asks about recent events, prices, news,
    schedules, scores, or anything that may not be in your training data, or
    when you need to verify a fact or cite a source. Returns the top organic
    results (title, URL, snippet) and a direct answer when one is available.
    """
    _require_auth(authorization)

    if not SERPER_API_KEY:
        raise HTTPException(status_code=503, detail="SERPER_API_KEY is not configured")

    payload = {"q": body.query, "num": body.num}
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(SERPER_ENDPOINT, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()
    except aiohttp.ClientResponseError as exc:
        raise HTTPException(status_code=502, detail=f"Serper error: {exc.status}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Serper request failed: {type(exc).__name__}")

    # Prefer a direct answer when Serper provides one (answer box / knowledge graph).
    answer = None
    answer_box = data.get("answerBox") or {}
    if isinstance(answer_box, dict):
        answer = answer_box.get("answer") or answer_box.get("snippet")
    if not answer:
        kg = data.get("knowledgeGraph") or {}
        if isinstance(kg, dict):
            answer = kg.get("description")

    results: list[SearchResult] = []
    for item in (data.get("organic") or [])[: body.num]:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        url = (item.get("link") or "").strip()
        snippet = (item.get("snippet") or item.get("description") or "").strip()
        if title or url:
            results.append(SearchResult(title=title or url, url=url, snippet=snippet))

    return SearchResponse(query=body.query, answer=answer, results=results)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8093")))
