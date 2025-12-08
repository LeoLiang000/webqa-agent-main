import asyncio
import logging
from typing import List, Optional

from webqa_agent.browser.session import BrowserSession


class BrowserSessionPool:
    """Browser session pool:
        - Simple API (initialize/acquire/release/close_all)
        - Queue is the only concurrency control
        - Lazy recovery: only recreate a session when caller marks it failed
        - Lazy initialization: creates sessions on-demand
    """

    def __init__(self, pool_size: int = 2, browser_config: Optional[dict] = None):
        """ 创建池对象(不启动浏览器) """
        if pool_size <= 0:
            raise ValueError("pool_size must > 0")

        self.pool_size = pool_size
        self.browser_config = browser_config or {}

        self._available_sessions: asyncio.Queue[BrowserSession] = asyncio.Queue(maxsize=pool_size)
        self._sessions: List[BrowserSession] = []
        self._session_counter = 0  # 用于生成唯一 session ID

        self._initialized = False
        self._closed = False
        self._creation_lock = asyncio.Lock()  # 控制并发创建

    async def initialize(self) -> "BrowserSessionPool":
        """ 初始化池（不创建浏览器，只标记为已初始化） """
        if self._initialized:
            return self
        if self._closed:
            raise RuntimeError("BrowserSessionPool has been closed")

        self._initialized = True
        logging.info(f"[SessionPool] Initialized (lazy mode, max_size={self.pool_size})")
        return self

    async def _create_session(self) -> BrowserSession:
        """创建新 session（线程安全）"""
        s = BrowserSession(
            session_id=f"pool_session_{self._session_counter}",
            browser_config=self.browser_config
        )
        self._session_counter += 1
        await s.initialize()
        self._sessions.append(s)
        logging.info(f"[SessionPool] Created session: {s.session_id} (total: {len(self._sessions)}/{self.pool_size})")
        return s

    async def acquire(self, timeout: Optional[float] = 60.0) -> BrowserSession:
        """从池获取 session（懒加载：队列空时按需创建）"""
        if not self._initialized:
            raise RuntimeError("BrowserSessionPool not initialized")
        if self._closed:
            raise RuntimeError("BrowserSessionPool has been closed")

        # 快速检查：如果队列为空且未达上限，直接创建
        if self._available_sessions.empty():
            async with self._creation_lock:
                if len(self._sessions) < self.pool_size:
                    return await self._create_session()

        # 否则从队列获取（可能需要等待）
        if timeout is None:
            return await self._available_sessions.get()
        return await asyncio.wait_for(self._available_sessions.get(), timeout=timeout)

    async def release(self, session: BrowserSession, failed: bool = False) -> None:
        """ 把 session 还回池；如果该次使用失败，则重建后再还回池 """
        if self._closed or session is None:
            return

        if failed or session.is_closed():
            session = await self._recover(session)

        try:
            self._available_sessions.put_nowait(session)
        except asyncio.QueueFull as e:
            raise RuntimeError("Session pool is full") from e

    async def _recover(self, session: BrowserSession) -> BrowserSession:
        """ 重置干净的session，销毁旧 session 并创建同 ID 的新 session，替换池内引用 """
        session_id = getattr(session, "session_id", "unknown")
        logging.info(f"[SessionPool] Recovering session: {session_id}")

        try:
            await session.close()
        except Exception:
            logging.exception(f"[SessionPool] Failed to close session {session_id}")

        new_s = BrowserSession(session_id=session_id, browser_config=self.browser_config)  # 用旧ID创建新session
        await new_s.initialize()

        try:
            idx = self._sessions.index(session)
            self._sessions[idx] = new_s  # 用新session替换旧session
        except ValueError:
            self._sessions.append(new_s)

        return new_s

    async def close_all(self) -> None:
        """ 关闭池里所有 session，释放资源 """
        if self._closed:
            return
        self._closed = True

        await asyncio.gather(*[s.close() for s in self._sessions], return_exceptions=True)  # 并发关闭sessions
        self._sessions.clear()

        while not self._available_sessions.empty():
            try:
                self._available_sessions.get_nowait()  # 清空队列
            except Exception:
                break

        logging.info("[SessionPool] Closed")

    # 让 pool 支持 async with 管理生命周期，简化外层资源管理，避免忘记关闭导致浏览器泄漏
    async def __aenter__(self):
        if not self._initialized:
            await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close_all()
