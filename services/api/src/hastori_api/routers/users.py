"""User management (system admin)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from hastori_api.deps import Scope, Session
from hastori_api.errors import COMMON_ERRORS, ErrorResponse
from hastori_api.queries import users
from hastori_api.schemas import MAX_PAGE_SIZE, Page, UserCreate, UserOut, UserPatch

router = APIRouter(prefix="/users", tags=["users"], responses=COMMON_ERRORS)


@router.get("", response_model=Page[UserOut], summary="Users of the organisation (system admin)")
async def list_users(
    scope: Scope,
    session: Session,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> Page[UserOut]:
    items, total = await users.list_users(session, scope, page, size)
    return Page[UserOut](items=items, total=total, page=page, size=size)


@router.post(
    "",
    response_model=UserOut,
    status_code=201,
    summary="Create a user (system admin)",
    description="The password needs at least 12 characters. The password is never returned.",
    responses={409: {"model": ErrorResponse, "description": "E-mail address already in use"}},
)
async def create_user(body: UserCreate, scope: Scope, session: Session) -> UserOut:
    return await users.create_user(session, scope, body)


@router.get("/{user_id}", response_model=UserOut, summary="One user (system admin)")
async def get_user(user_id: uuid.UUID, scope: Scope, session: Session) -> UserOut:
    return await users.get_user(session, scope, user_id)


@router.patch(
    "/{user_id}",
    response_model=UserOut,
    summary="Change role, sites or password (system admin)",
    responses={409: {"model": ErrorResponse, "description": "The last system admin"}},
)
async def patch_user(
    user_id: uuid.UUID, body: UserPatch, scope: Scope, session: Session
) -> UserOut:
    return await users.patch_user(session, scope, user_id, body)
