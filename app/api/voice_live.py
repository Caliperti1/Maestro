"""GPT-Live session negotiation for the Maestro Voice iOS client."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.maestro.live_voice import (
    GPTLiveSessionService,
    LiveVoiceInvalidResponseError,
    LiveVoiceNotConfiguredError,
    LiveVoiceUpstreamError,
)

router = APIRouter(prefix="/maestro/voice/live", tags=["maestro-voice"])


class LiveSessionRequest(BaseModel):
    sdp: str = Field(min_length=10, max_length=100_000)
    client_identifier: str = Field(min_length=1, max_length=240)


class LiveSessionResponse(BaseModel):
    session_id: str
    sdp: str
    model: str


def get_live_session_service() -> GPTLiveSessionService:
    return GPTLiveSessionService(get_settings())


@router.post("/session", response_model=LiveSessionResponse, status_code=201)
async def create_live_session(
    body: LiveSessionRequest,
    service: Annotated[GPTLiveSessionService, Depends(get_live_session_service)],
) -> LiveSessionResponse:
    try:
        session = await service.create_session(
            sdp=body.sdp,
            client_identifier=body.client_identifier,
        )
    except LiveVoiceNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LiveVoiceUpstreamError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc
    except LiveVoiceInvalidResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return LiveSessionResponse(
        session_id=session.session_id,
        sdp=session.sdp,
        model=session.model,
    )
