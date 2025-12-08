import logging
import uuid
import os

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Coroutine

from webqa_agent.browser.config import DEFAULT_CONFIG
from webqa_agent.browser.session_pool import BrowserSessionPool
from webqa_agent.data import ParallelTestSession, TestConfiguration, TestType, get_default_test_name
from webqa_agent.executor.test_runners import UIAgentLangGraphRunner
from webqa_agent.utils import Display
from webqa_agent.utils.get_log import GetLog
from webqa_agent.utils.log_icon import icon


class Executor:
    """
    WebqaAgentExecutor class: parallely execute testcases
    """

    def __init__(self,
                 max_concurrent: int,
                 url: str,
                 test_cfg: list,
                 llm_cfg: dict,
                 log_cfg: dict,
                 report_cfg: dict,
                 browser_cfg: Optional[Dict[str, Any]] = None,
                 ):
        self.max_concurrent = max_concurrent
        self.url = url
        self.test_cfg = test_cfg
        self.llm_cfg = llm_cfg
        self.log_cfg = log_cfg
        self.report_cfg = report_cfg
        self.browser_cfg = browser_cfg
        self.sessions = None  # 系统侧
        self.test_session = None  # 业务侧

    async def initialize(self):
        """
        initialize all test configurations for webqa_agent_executor
        """
        GetLog.get_log(log_level=self.log_cfg['level'])
        Display.init(language=self.report_cfg["language"])
        Display.display.start()
        logging.info(f"{icon['rocket']} Starting tests for URL: {self.url}, parallel mode {self.max_concurrent}")

        if not self.browser_cfg:
            self.browser_cfg = DEFAULT_CONFIG.copy()

        # create and initialize session pool
        self.sessions = BrowserSessionPool(pool_size=self.max_concurrent, browser_config=self.browser_cfg)
        await self.sessions.initialize()

        # create test session
        self.test_session = ParallelTestSession(session_id=str(uuid.uuid4()),
                                                target_url=self.url,
                                                llm_config=self.llm_cfg)

        # Use a fresh per-task timestamp for reports and keep logs separate
        os.environ["WEBQA_REPORT_TIMESTAMP"] = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")

        # Configure tests based on input
        if self.test_cfg:
            self._configure_tests_from_config(self.test_session)

    def _configure_tests_from_config(self, test_session: ParallelTestSession):
        """Configure tests from provided configuration."""

        def _map_test_type(test_type_str: str) -> TestType:
            """Map string to TestType enum."""
            mapping = {
                "ui_agent_langgraph": TestType.UI_AGENT_LANGGRAPH,
                # "ux_test": TestType.UX_TEST,  # to be deleted
                # "performance": TestType.PERFORMANCE,  # to be deleted
                # "basic_test": TestType.BASIC_TEST,  # to be deleted
                # "security": TestType.SECURITY_TEST,  # to be deleted
                # "security_test": TestType.SECURITY_TEST,  # to be deleted
            }

            return mapping.get(test_type_str, TestType.BASIC_TEST)

        for cfg in self.test_cfg:
            # Map string to TestType enum
            test_type = _map_test_type(cfg.get("test_type", "basic_test"))

            # Merge browser config
            browser_cfg = {**self.browser_cfg, **cfg.get("browser_config", {})}

            test_cfg = TestConfiguration(
                test_id=str(uuid.uuid4()),
                test_type=test_type,
                test_name=get_default_test_name(test_type, self.report_cfg["language"]),
                enabled=cfg.get("enabled", True),
                browser_config=browser_cfg,
                report_config=self.report_cfg,
                test_specific_config=cfg.get("test_specific_config", {}),
                timeout=cfg.get("timeout", 300),
                retry_count=cfg.get("retry_count", 0),
                dependencies=cfg.get("dependencies", []),
            )

            self.test_session.add_test_configuration(test_cfg)


    async def prepare(self):
        """ Identify test type from test config, prepare browser sessions and execute tests """
        for test_config in self.test_session.test_configurations:
            try:
                s = await self.sessions.acquire()
                runner = UIAgentLangGraphRunner()
                result = await runner.run_test(
                    session=s,  # 传 pool
                    test_config=test_config,
                    llm_config=self.llm_cfg,
                    target_url=self.url,
                )

            except Exception as e:
                logging.error(f"{icon['cross']} Test execution failed: {test_config.test_name}, Error: {e}")
                import traceback
                traceback.print_exc()

            finally:
                await self.sessions.release(s)

    async def execute(self):
        pass

    async def run(self):
        # Initialize webqa_agent_executor
        try:
            await self.initialize()
            logging.info(f"{icon['check']} Successfully initialized webqa_agent_executor: {self.url}")
        except Exception as e:
            logging.error(f"{icon['cross']} Failed to initialize webqa_agent_executor: {e}")

        # Prepare browser sessions
        await self.prepare()

        # UIAgentLanggraphRunner ....

        # Execute
        # self.execute()
