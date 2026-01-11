import json
import logging
import os
import re
import uuid
from pathlib import Path

import jwt
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

LIVEKIT_DIR = Path(__file__).resolve().parent
HTML_PATH = LIVEKIT_DIR / "index.html"
MANIFEST_PATH = LIVEKIT_DIR / "manifest.webmanifest"
SW_PATH = LIVEKIT_DIR / "sw.js"
ICONS_DIR = LIVEKIT_DIR / "icons"
KEY_FILE = Path.cwd() / ".webui_secret_key"

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
LIVEKIT_HTTP_URL = os.environ.get("LIVEKIT_HTTP_URL", "http://127.0.0.1:7880").strip()

AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "owui-voice")
ROOM_PREFIX = os.getenv("LIVEKIT_ROOM_PREFIX", "owui-voice")
ROOM_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

logger = logging.getLogger("open_webui.livekit.portal")


def _require_env(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _get_webui_secret_key() -> str:
    secret = os.environ.get("WEBUI_SECRET_KEY", "").strip()
    if secret:
        return secret

    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip()

    return ""


def _get_livekit():
    from livekit import api

    return api


def _build_voice_settings(
    *,
    llm_model: str | None,
    turn_detection: str | None,
    allow_interruptions: bool | None,
    min_endpointing_delay: float | None,
    max_endpointing_delay: float | None,
    min_interruption_duration: float | None,
    min_interruption_words: int | None,
    tts_voice: str | None,
) -> dict[str, object]:
    voice_settings: dict[str, object] = {}

    if llm_model is not None:
        normalized_llm_model = llm_model.strip()
        if normalized_llm_model:
            lowered = normalized_llm_model.lower()
            if lowered in ("4.6", "glm4.6", "glm-4.6", "zai-glm-4.6"):
                voice_settings["llm_model"] = "zai-glm-4.6"
            elif lowered in ("4.7", "glm4.7", "glm-4.7", "zai-glm-4.7"):
                voice_settings["llm_model"] = "zai-glm-4.7"
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid llm_model (allowed: glm-4.6, glm-4.7)",
                )

    if turn_detection:
        normalized_turn_detection = turn_detection.strip().lower()
        if normalized_turn_detection in ("ml", "multilingual"):
            voice_settings["turn_detection"] = "ml"
        elif normalized_turn_detection == "stt":
            voice_settings["turn_detection"] = "stt"
        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid turn_detection (allowed: stt, ml)",
            )

    if allow_interruptions is not None:
        voice_settings["allow_interruptions"] = allow_interruptions
    if min_endpointing_delay is not None:
        voice_settings["min_endpointing_delay"] = min_endpointing_delay
    if max_endpointing_delay is not None:
        voice_settings["max_endpointing_delay"] = max_endpointing_delay
    if min_interruption_duration is not None:
        voice_settings["min_interruption_duration"] = min_interruption_duration
    if min_interruption_words is not None:
        voice_settings["min_interruption_words"] = min_interruption_words
    if tts_voice is not None:
        normalized_tts_voice = tts_voice.strip()
        if normalized_tts_voice:
            if len(normalized_tts_voice) > 256:
                raise HTTPException(status_code=400, detail="tts_voice is too long")
            voice_settings["tts_voice"] = normalized_tts_voice

    if (
        min_endpointing_delay is not None
        and max_endpointing_delay is not None
        and min_endpointing_delay > max_endpointing_delay
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid endpointing delays (min_endpointing_delay > max_endpointing_delay)",
        )

    return voice_settings


app = FastAPI(title="Open WebUI LiveKit Portal")


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(
        HTML_PATH.read_text(encoding="utf-8"),
        headers={
            # Avoid stale HTML in browsers/service workers; this page changes frequently while iterating.
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(
        MANIFEST_PATH,
        media_type="application/manifest+json",
        headers={
            # Keep install metadata fresh; changes should be picked up quickly.
            "Cache-Control": "no-cache",
        },
    )


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        SW_PATH,
        media_type="application/javascript",
        headers={
            # Service worker updates should always revalidate.
            "Cache-Control": "no-cache",
        },
    )


@app.get("/icons/{filename}")
async def icon(filename: str):
    if filename not in ("icon-192.png", "icon-512.png"):
        raise HTTPException(status_code=404, detail="Not found")

    path = ICONS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")

    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
        },
    )


@app.get("/token")
async def token(
    room: str | None = None,
    llm_model: str | None = Query(default=None),
    turn_detection: str | None = Query(default=None),
    allow_interruptions: bool | None = Query(default=None),
    min_endpointing_delay: float | None = Query(default=None, ge=0.0, le=10.0),
    max_endpointing_delay: float | None = Query(default=None, ge=0.0, le=10.0),
    min_interruption_duration: float | None = Query(default=None, ge=0.0, le=10.0),
    min_interruption_words: int | None = Query(default=None, ge=0, le=50),
    tts_voice: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    webui_secret_key = _get_webui_secret_key()
    _require_env("WEBUI_SECRET_KEY (or .webui_secret_key file)", webui_secret_key)
    _require_env("LIVEKIT_URL", LIVEKIT_URL)
    _require_env("LIVEKIT_API_KEY", LIVEKIT_API_KEY)
    _require_env("LIVEKIT_API_SECRET", LIVEKIT_API_SECRET)

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    auth_token = authorization.removeprefix("Bearer ").strip()
    try:
        decoded = jwt.decode(auth_token, webui_secret_key, algorithms=["HS256"])
    except Exception:
        decoded = None

    if not decoded or "id" not in decoded:
        raise HTTPException(status_code=401, detail="Invalid Open WebUI token")

    user_id = str(decoded["id"])

    room_name = (room or "").strip()
    if not room_name:
        room_name = f"{ROOM_PREFIX}-{uuid.uuid4().hex}"
    elif not ROOM_NAME_RE.fullmatch(room_name) or not room_name.startswith(f"{ROOM_PREFIX}-"):
        raise HTTPException(status_code=400, detail="Invalid room name")

    api = _get_livekit()

    grants = api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )

    # Explicit agent dispatch: only dispatch the agent when Open WebUI mints a token.
    from livekit.protocol.agent_dispatch import RoomAgentDispatch
    from livekit.protocol.room import RoomConfiguration

    voice_settings = _build_voice_settings(
        llm_model=llm_model,
        turn_detection=turn_detection,
        allow_interruptions=allow_interruptions,
        min_endpointing_delay=min_endpointing_delay,
        max_endpointing_delay=max_endpointing_delay,
        min_interruption_duration=min_interruption_duration,
        min_interruption_words=min_interruption_words,
        tts_voice=tts_voice,
    )

    room_config = RoomConfiguration(agents=[RoomAgentDispatch(agent_name=AGENT_NAME)])
    metadata = (
        json.dumps({"owui_voice": voice_settings}, separators=(",", ":"), sort_keys=True)
        if voice_settings
        else ""
    )

    # Always set metadata in the room config so newly-created rooms get the correct settings.
    # Note: RoomConfiguration metadata does NOT update existing rooms, so we also update it via RoomService below.
    room_config.metadata = metadata

    access_token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    access_token.with_identity(f"owui:{user_id}")
    access_token.with_name(f"Open WebUI User {user_id}")
    access_token.with_metadata(f'{{"open_webui_user_id":"{user_id}"}}')
    access_token.with_grants(grants)
    access_token.with_room_config(room_config)

    # Important: when you reuse an existing room name, LiveKit keeps the old room metadata.
    # That means "Turn Detection: STT-only" (or any other setting) can look "broken" because
    # the agent still sees the metadata from the first time the room was created.
    #
    # Fix: update the room metadata if the room already exists.
    try:
        lk = api.LiveKitAPI(
            url=LIVEKIT_HTTP_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )
        try:
            existing = await lk.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
            if existing.rooms:
                await lk.room.update_room_metadata(
                    api.UpdateRoomMetadataRequest(room=room_name, metadata=metadata)
                )

                # If the agent job is already running, it will not pick up the new room metadata.
                # Restart the dispatch when settings have changed so toggles (e.g. STT-only) actually apply.
                try:
                    from livekit.protocol import agent_dispatch as agent_dispatch_proto

                    dispatches = await lk.agent_dispatch.list_dispatch(room_name=room_name)
                    agent_dispatches = [
                        d for d in dispatches if getattr(d, "agent_name", None) == AGENT_NAME
                    ]

                    restart_needed = False
                    if not agent_dispatches:
                        restart_needed = True
                    else:
                        for d in agent_dispatches:
                            jobs = getattr(getattr(d, "state", None), "jobs", None) or []
                            for job in jobs:
                                job_room = getattr(job, "room", None)
                                job_metadata = getattr(job_room, "metadata", None) or ""
                                if job_metadata != metadata:
                                    restart_needed = True
                                    break
                            if restart_needed:
                                break

                    if restart_needed:
                        # Avoid disrupting an active room with non-agent participants.
                        participants = await lk.room.list_participants(
                            api.ListParticipantsRequest(room=room_name)
                        )
                        has_non_agent = any(
                            not str(getattr(p, "identity", "")).startswith("agent-")
                            for p in (participants.participants or [])
                        )
                        if has_non_agent:
                            logger.warning(
                                "Skipping agent dispatch restart (room has active non-agent participants)",
                                extra={"room": room_name},
                            )
                        else:
                            for d in agent_dispatches:
                                await lk.agent_dispatch.delete_dispatch(d.id, room_name)
                            await lk.agent_dispatch.create_dispatch(
                                agent_dispatch_proto.CreateAgentDispatchRequest(
                                    agent_name=AGENT_NAME, room=room_name, metadata=""
                                )
                            )
                except Exception:
                    logger.exception("Failed to restart agent dispatch", extra={"room": room_name})
        finally:
            await lk.aclose()
    except Exception:
        logger.exception(
            "Failed to update LiveKit room metadata",
            extra={"room": room_name, "livekit_http_url": LIVEKIT_HTTP_URL},
        )

    return {
        "url": LIVEKIT_URL,
        "room": room_name,
        "token": access_token.to_jwt(),
        "identity": f"owui:{user_id}",
    }


@app.post("/apply")
async def apply(
    room: str,
    llm_model: str | None = Query(default=None),
    turn_detection: str | None = Query(default=None),
    allow_interruptions: bool | None = Query(default=None),
    min_endpointing_delay: float | None = Query(default=None, ge=0.0, le=10.0),
    max_endpointing_delay: float | None = Query(default=None, ge=0.0, le=10.0),
    min_interruption_duration: float | None = Query(default=None, ge=0.0, le=10.0),
    min_interruption_words: int | None = Query(default=None, ge=0, le=50),
    tts_voice: str | None = Query(default=None),
    force: bool = Query(default=False),
    authorization: str | None = Header(default=None),
):
    webui_secret_key = _get_webui_secret_key()
    _require_env("WEBUI_SECRET_KEY (or .webui_secret_key file)", webui_secret_key)
    _require_env("LIVEKIT_URL", LIVEKIT_URL)
    _require_env("LIVEKIT_API_KEY", LIVEKIT_API_KEY)
    _require_env("LIVEKIT_API_SECRET", LIVEKIT_API_SECRET)

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    auth_token = authorization.removeprefix("Bearer ").strip()
    try:
        decoded = jwt.decode(auth_token, webui_secret_key, algorithms=["HS256"])
    except Exception:
        decoded = None

    if not decoded or "id" not in decoded:
        raise HTTPException(status_code=401, detail="Invalid Open WebUI token")

    user_id = str(decoded["id"])

    room_name = (room or "").strip()
    if not room_name:
        raise HTTPException(status_code=400, detail="Room is required")
    if not ROOM_NAME_RE.fullmatch(room_name) or not room_name.startswith(f"{ROOM_PREFIX}-"):
        raise HTTPException(status_code=400, detail="Invalid room name")

    voice_settings = _build_voice_settings(
        llm_model=llm_model,
        turn_detection=turn_detection,
        allow_interruptions=allow_interruptions,
        min_endpointing_delay=min_endpointing_delay,
        max_endpointing_delay=max_endpointing_delay,
        min_interruption_duration=min_interruption_duration,
        min_interruption_words=min_interruption_words,
        tts_voice=tts_voice,
    )
    metadata = (
        json.dumps({"owui_voice": voice_settings}, separators=(",", ":"), sort_keys=True)
        if voice_settings
        else ""
    )

    api = _get_livekit()
    try:
        lk = api.LiveKitAPI(
            url=LIVEKIT_HTTP_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )
        try:
            existing = await lk.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
            if not existing.rooms:
                raise HTTPException(status_code=404, detail="Room not found")

            await lk.room.update_room_metadata(
                api.UpdateRoomMetadataRequest(room=room_name, metadata=metadata)
            )

            participants = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name))
            other_humans = [
                str(getattr(p, "identity", ""))
                for p in (participants.participants or [])
                if not str(getattr(p, "identity", "")).startswith("agent-")
                and str(getattr(p, "identity", "")) != f"owui:{user_id}"
            ]
            if other_humans and not force:
                raise HTTPException(
                    status_code=409,
                    detail="Room has other participants; pass force=true to restart the agent anyway",
                )

            # Restart the dispatch so the already-running job picks up the new metadata.
            try:
                from livekit.protocol import agent_dispatch as agent_dispatch_proto

                dispatches = await lk.agent_dispatch.list_dispatch(room_name=room_name)
                for d in dispatches:
                    if getattr(d, "agent_name", None) == AGENT_NAME:
                        await lk.agent_dispatch.delete_dispatch(d.id, room_name)

                await lk.agent_dispatch.create_dispatch(
                    agent_dispatch_proto.CreateAgentDispatchRequest(
                        agent_name=AGENT_NAME, room=room_name, metadata=""
                    )
                )
            except Exception:
                logger.exception("Failed to restart agent dispatch", extra={"room": room_name})
                raise HTTPException(status_code=500, detail="Failed to restart agent dispatch")
        finally:
            await lk.aclose()
    except HTTPException:
        raise
    except Exception:
        logger.exception("Apply failed", extra={"room": room_name})
        raise HTTPException(status_code=500, detail="Apply failed")

    return {"ok": True, "room": room_name, "metadata": metadata}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8091")))
