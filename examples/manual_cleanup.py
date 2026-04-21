import asyncio
from colab_automation.session import ColabSession, _is_connected
from colab_automation.js import STATUS_JS

async def main():
    async with ColabSession(cdp_port=9223) as session:
        for page in session._ctx.pages:
            if "colab.research.google.com" not in page.url:
                continue
            if _is_connected(await page.evaluate(STATUS_JS)):
                await session.disconnect_and_delete_runtime(page)

asyncio.run(main())
