"""
Base connector providing shared Google API token injection and
thread-pool execution for synchronous SDK calls.

CRITICAL: Google SDK clients (gRPC-based) make synchronous blocking calls.
Running them directly in async functions blocks the entire event loop.
All sync SDK calls MUST go through run_sync() to execute in a thread pool.
"""

import asyncio
import logging
import random
from abc import ABC
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar

from app.auth.google_token_manager import GoogleTokenManager

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Shared thread pool for all connector sync calls.
# Size 20 allows up to 20 concurrent Google API calls without blocking the event loop.
_SDK_THREAD_POOL_SIZE = 20
_sdk_thread_pool = ThreadPoolExecutor(max_workers=_SDK_THREAD_POOL_SIZE, thread_name_prefix="google-sdk")

# Retryable error patterns for transient API failures (pre-computed for performance)
_RETRYABLE_ERROR_CODES = frozenset(
    {"429", "503", "unavailable", "deadline exceeded", "resource exhausted", "connection reset"}
)


class BaseConnector(ABC):
    def __init__(self, token_manager: GoogleTokenManager):
        self.token_manager = token_manager

    async def get_token(self, connection_id: str) -> str:
        return await self.token_manager.get_valid_access_token(connection_id)

    @staticmethod
    async def run_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """
        Run a synchronous function in the thread pool.
        Use this for ALL Google SDK calls that are synchronous (gRPC-based).

        Usage:
            result = await self.run_sync(client.run_report, request=request_obj)
        """
        loop = asyncio.get_event_loop()
        if kwargs:
            func_with_kwargs = partial(func, *args, **kwargs)
            return await loop.run_in_executor(_sdk_thread_pool, func_with_kwargs)
        elif args:
            return await loop.run_in_executor(_sdk_thread_pool, partial(func, *args))
        else:
            return await loop.run_in_executor(_sdk_thread_pool, func)

    @staticmethod
    async def run_sync_with_retry(
        func: Callable[..., T],
        *args: Any,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> T:
        """
        Run a sync function in the thread pool with exponential backoff retry
        on transient errors (429, 503, connection errors).
        """
        loop = asyncio.get_event_loop()
        last_exc = None

        for attempt in range(max_retries):
            try:
                if kwargs:
                    return await loop.run_in_executor(_sdk_thread_pool, partial(func, *args, **kwargs))
                elif args:
                    return await loop.run_in_executor(_sdk_thread_pool, partial(func, *args))
                else:
                    return await loop.run_in_executor(_sdk_thread_pool, func)
            except Exception as e:
                last_exc = e
                error_str = str(e).lower()
                is_retryable = any(code in error_str for code in _RETRYABLE_ERROR_CODES)

                if not is_retryable or attempt == max_retries - 1:
                    raise

                wait = (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    f"Retryable error (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait:.1f}s..."
                )
                await asyncio.sleep(wait)

        raise last_exc  # Should never reach here
