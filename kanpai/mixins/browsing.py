import contextlib
import urllib.parse
from typing import Optional, TYPE_CHECKING

from kani import ChatMessage, ai_function
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from kanpai import events
from kanpai.base_kani import BaseKani
from kanpai.state import RunState
from kanpai.webutils import get_google_links, web_markdownify, web_summarize

if TYPE_CHECKING:
    from playwright.async_api import Page


class BrowsingMixin(BaseKani):
    def __init__(self, *args, max_webpage_len: int = 1024, **kwargs):
        super().__init__(*args, **kwargs)
        self.page: Optional["Page"] = None
        self.max_webpage_len = max_webpage_len  # the max number of tokens before asking for a summary

    # browser management
    async def get_page(self, create=True) -> Optional["Page"]:
        """Get the current page.

        Returns None if the browser is not on a page unless `create` is True, in which case it creates a new page.
        """
        if self.page is None and create:
            context = await self.app.get_browser()
            self.page = await context.new_page()
        return self.page

    async def cleanup(self):
        await super().cleanup()
        if self.page is not None:
            await self.page.close()
            self.page = None

    # functions
    @ai_function()
    async def search(self, query: str):
        """Search a query on Google."""
        page = await self.get_page()
        query_enc = urllib.parse.quote_plus(query)
        await page.goto(f"https://www.google.com/search?q={query_enc}")
        # content
        try:
            # if the main content is borked, fallback
            search_html = await page.inner_html("#main", timeout=5000)
            search_text = web_markdownify(search_html, include_links=False)
            # links
            search_loc = page.locator("#search")
            links = await get_google_links(search_loc)
            return (
                f"{search_text.strip()}\n\nYou should visit some of these links for more information or delegate"
                f" helpers to visit multiple:\n\n===== Links =====\n{links.to_md_str()}"
            )
        except PlaywrightTimeoutError:
            content_html = await page.content()
            content = web_markdownify(content_html)
            return content

    @ai_function()
    async def visit_page(self, href: str):
        """Visit a web page and view its contents."""
        page = await self.get_page()
        await page.goto(href)
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state("networkidle", timeout=10_000)
        # header
        title = await page.title()
        header = f"{title}\n{'=' * len(title)}\n{page.url}\n\n"
        # content
        content_html = await page.content()
        content = web_markdownify(content_html)
        # summarization
        if self.message_token_len(ChatMessage.function("visit_page", content)) > self.max_webpage_len:
            msg_ctx = "\n\n".join(m.text for m in self.chat_history if m.text is not None)
            content = await web_summarize(
                content,
                parent=self,
                task=(
                    "Keep the current context in mind:\n"
                    f"<context>\n{msg_ctx}\n</context>\n\n"
                    "Keeping the context and task in mind, please summarize the main content of the webpage above."
                ),
            )
        result = header + content
        return result

    @ai_function()
    async def blocked(self):
        """Call this function if a page visit was blocked by the page."""
        self.state = RunState.ERRORED
        self.app.dispatch(events.KaniStateChange(id=self.id, state=self.state))
        return "The block has been noted. Don't use this site for future queries."
