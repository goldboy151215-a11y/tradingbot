import asyncio
import json
import logging
import os
import time
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

CACHE_FILE = "/root/tradovate_bot/token_cache.json"

import base64

def extract_jwt_exp(token: str) -> float:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(payload.get("exp", time.time() + 3600))
    except Exception:
        return time.time() + 3600

class AuthManager:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.access_token = None
        self.md_token = None
        self.expiration_time = 0

    def load_cached(self) -> bool:
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r") as f:
                    data = json.load(f)
                acc = data.get("access_token")
                md = data.get("md_token")
                if acc:
                    exp = extract_jwt_exp(acc)
                    # Ensure at least 10 minutes validity left
                    if time.time() < exp - 600:
                        self.access_token = acc
                        self.md_token = md or acc
                        self.expiration_time = exp
                        mins_left = (exp - time.time()) / 60
                        logger.info(f"Loaded valid Tradovate tokens from cache (valid for another {mins_left:.1f} mins)")
                        return True
            except Exception as e:
                logger.warning(f"Error loading cache: {e}")
        return False

    def save_cache(self):
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump({
                    "access_token": self.access_token,
                    "md_token": self.md_token,
                    "expiration_time": self.expiration_time
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"Error saving cache: {e}")

    async def get_tokens(self, force_refresh: bool = False) -> tuple[str, str]:
        if not force_refresh and self.load_cached():
            return self.access_token, self.md_token

        logger.info("Requesting fresh Tradovate tokens via headless session...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})

            acc_tok, md_tok = None, None
            async def on_resp(res):
                nonlocal acc_tok, md_tok
                if "tradovateapi.com" in res.url and "auth" in res.url:
                    try:
                        b = await res.json()
                        if "accessToken" in b:
                            acc_tok = b["accessToken"]
                        if "mdAccessToken" in b:
                            md_tok = b["mdAccessToken"]
                    except Exception:
                        pass

            page.on("response", on_resp)
            await page.goto("https://trader.tradovate.com/welcome", wait_until="networkidle", timeout=30000)
            await page.wait_for_selector("input", timeout=15000)

            inputs = await page.locator("input").all()
            await inputs[0].fill(self.username)
            await inputs[1].fill(self.password)
            await page.locator('button:has-text("Login")').click()
            await page.wait_for_url("**/trading-mode**", timeout=20000)

            # Click Access Simulation
            sim_btn = page.locator('button:has-text("Access Simulation")')
            if await sim_btn.count() > 0:
                await sim_btn.first.click()

            await asyncio.sleep(4)
            await browser.close()

            if not acc_tok:
                raise RuntimeError("Failed to obtain Tradovate accessToken from login flow!")

            self.access_token = acc_tok
            self.md_token = md_tok or acc_tok
            self.expiration_time = extract_jwt_exp(acc_tok)
            self.save_cache()
            logger.info("Successfully refreshed and cached Tradovate tokens!")
            return self.access_token, self.md_token
