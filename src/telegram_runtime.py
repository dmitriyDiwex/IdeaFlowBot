from contextlib import asynccontextmanager


@asynccontextmanager
async def telegram_process_session(_server=None):
    """pyTelegramBotAPI shares a session across bots in one thread/event loop.

    Own it for the process lifespan, never close it after an individual request:
    that cancels concurrent calls made by unrelated proposal bots.
    """
    from telebot import asyncio_helper
    from src.editorial.config import settings

    asyncio_helper.REQUEST_TIMEOUT = settings.telegram_request_timeout_seconds
    asyncio_helper.REQUEST_LIMIT = max(asyncio_helper.REQUEST_LIMIT, settings.mcp_worker_concurrency * 4)
    try:
        yield
    finally:
        session = asyncio_helper.session_manager.session
        if session is not None and not session.closed:
            await session.close()
        asyncio_helper.session_manager.session = None


def calculate_telegram_request_limit(
    current_subbot_count: int,
    max_subbot_count: int,
    configured_limit: int,
    connections_per_bot: int = 4,
    overhead_connections: int = 20,
) -> int:
    target_subbot_count = max(current_subbot_count, max_subbot_count, 0)
    active_bot_count = target_subbot_count + 1
    required_limit = active_bot_count * connections_per_bot + overhead_connections
    return max(configured_limit, required_limit)
