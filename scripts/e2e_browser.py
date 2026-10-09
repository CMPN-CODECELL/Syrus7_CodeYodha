"""Real-mode browser journey against a running stack (scripts/dev.py), the way a new trader meets Countersign:
landing page, Explore while signed out, sign up, onboarding (weak PIN refused, chosen PIN, passkey, authenticator
app), a read-only question, a passkey countersign, a PIN countersign, the Attack Lab, the Security Center with a kill
switch engage and release, sign out, sign in with the password, sign in with the passkey, the signed-out redirect,
and API checks for the seeded paper account and cross-user isolation. Passkeys come from a Chrome virtual
authenticator (CDP). Fails on page errors and on any unexpected 4xx/5xx API response. Cleans up after itself: the
passkey it registered is revoked. Order prices come from the live quote, so the journey works on the replay feed and
on live 021 prices alike.

--broker 021 signs in instead as an existing user linked to the 021 sandbox account (email, password and PIN read
from --login-file, never printed): L1 questions on the live account, the official standing-instruction example (its
condition is already true at real prices, so the copilot must ask), and a 1-share order countersigned with the PIN
and then cancelled through the copilot. The order is checked in the 021 order book through the gateway. The 021
sandbox may fill an order before the cancellation arrives; that is reported, not hidden.

Needs Python Playwright once: pip install playwright httpx && playwright install chromium
Run: python scripts/e2e_browser.py [--headed] [--device "Pixel 7"] [--base URL] [--broker 021 --login-file FILE]"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import random
import re
import secrets
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.sync_api import BrowserContext, Page, expect, sync_playwright

BASE = "http://localhost:3000"  # passkeys need the "localhost" RP ID
API = "http://127.0.0.1:3000"  # same Next.js proxy for out-of-browser checks; 127.0.0.1 skips the slow localhost lookup
SHOTS = Path(__file__).resolve().parents[1] / ".logs" / "e2e"
# Broker acknowledgement is the end of what a countersignature controls; a limit order may then rest unfilled.
DONE = re.compile("Broker acknowledged")
# Mirrors the paper broker's DEMO1 template (broker/app.py SEED_HOLDINGS and SEED_POSITIONS); every new account starts here.
SEED_HOLDINGS = {"RELIANCE": 50, "INFY": 100, "TCS": 20, "HDFCBANK": 60, "TATAMOTORS": 200, "SBIN": 150, "ITC": 300}
SEED_POSITIONS = {("ICICIBANK", "MIS"): 40}
SEED_CASH_PAISE = 25_00_000_00

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" :: {detail}" if detail else ""), flush=True)
    return ok


def weak_pin(pin: str) -> bool:
    """Same rule as the server's WEAK_PIN: one repeated digit, or a straight ascending or descending run."""
    steps = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
    return steps in ({0}, {1}, {-1})


def rupees(paise: int) -> str:
    """Whole rupees with Indian digit grouping (lakh, crore): 2500000 -> ₹25,00,000."""
    s = str(paise // 100)
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return "₹" + ",".join(([head] if head else []) + groups + [tail])


def totp_now(secret: str) -> str:
    """RFC 6238 (SHA-1, 6 digits, 30 s), what any authenticator app computes from the shown secret."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", int(time.time() // 30)), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


class Journey:
    def __init__(self, page: Page, ctx: BrowserContext, prefix: str) -> None:
        self.page = page
        self.ctx = ctx
        self.prefix = prefix
        tag = secrets.token_hex(4)
        self.name = "Asha E2E"
        self.email = f"e2e.{tag}@example.test"
        self.password = f"paper-trader-{secrets.token_hex(6)}"
        self.pin = next(p for p in (f"{random.randint(0, 9999):04d}" for _ in range(100)) if not weak_pin(p))
        self.signed_in = False
        self.proposals: list[str] = []
        self.conversation_id: str | None = None
        self.account_id = ""
        self.errors: list[str] = []
        self.failed_calls: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(str(e)[:300]))
        page.on("response", self._on_response)
        page.on("request", self._on_request)

    # ------------------------------------------------------------------ plumbing

    def _on_request(self, r) -> None:
        m = re.search(r"/api/proposals/([^/]+)/approval/start$", urlparse(r.url).path)
        if m and m.group(1) not in self.proposals:
            self.proposals.append(m.group(1))

    def _on_response(self, r) -> None:
        path = urlparse(r.url).path
        if r.status < 400 or not (path.startswith("/api/") or path.startswith("/api-stream/")):
            return
        # Signed out, /api/me answering 401 is how the app knows; queries cut off by sign-out may 401 too.
        if r.status == 401 and not self.signed_in and r.request.method == "GET":
            return
        # A backend without the 021 gateway endpoints answers 404 there; the UI falls back to the paper account.
        if r.status == 404 and path.startswith("/api/broker/") and r.request.method == "GET":
            return
        self.failed_calls.append(f"{r.status} {r.request.method} {path}")

    def shot(self, name: str, full: bool = False) -> None:
        try:
            self.page.screenshot(path=str(SHOTS / f"{self.prefix}_{name}.png"), full_page=full)
        except Exception:  # noqa: BLE001
            pass

    def csrf(self) -> str:
        return next((c["value"] for c in self.ctx.cookies(BASE) if c["name"] == "cs_csrf"), "")

    def api_get(self, path: str):
        r = self.page.request.get(BASE + path)
        return r.status, (r.json() if "json" in (r.headers.get("content-type") or "") else r.text())

    def api_post(self, path: str, body: dict | None = None):
        r = self.page.request.post(BASE + path, data=body or {}, headers={"X-CSRF-Token": self.csrf(), "Origin": BASE})
        return r.status, (r.json() if "json" in (r.headers.get("content-type") or "") else r.text())

    def no_overflow(self) -> bool:
        return self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

    def conversation(self):
        return self.page.locator('section[aria-label="Copilot conversation"]')

    def send(self, text: str) -> None:
        box = self.page.get_by_label("Message the copilot")
        box.fill(text)
        box.press("Enter")

    def account_menu(self, item: str) -> None:
        self.page.get_by_role("button", name=re.compile(r"^Account menu for")).click()
        self.page.get_by_role("menuitem", name=re.compile(item)).click()

    def sign_out(self) -> None:
        self.signed_in = False
        self.account_menu("Sign out")
        self.page.wait_for_url(BASE + "/", timeout=20000)

    def wait_copilot(self) -> None:
        self.page.wait_for_url(re.compile(r"/copilot"), timeout=30000)
        self.page.get_by_label("Message the copilot").wait_for(timeout=30000)

    def dones(self) -> int:
        return self.page.get_by_text(DONE).count()

    def wait_more_dones(self, before: int, timeout: float = 40) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if self.dones() > before:
                return
            self.page.wait_for_timeout(400)
        raise TimeoutError("no new 'Broker acknowledged' within %d s" % timeout)

    def use_engine(self, label: str) -> None:
        """Selects the copilot's engine in the composer."""
        p = self.page
        p.get_by_label("Model engine").click()
        p.get_by_role("option", name=label, exact=True).click()
        expect(p.get_by_label("Model engine")).to_contain_text(label, timeout=5000)

    def draft(self, text: str, summary: re.Pattern) -> None:
        conv = self.conversation()
        self.send(text)
        conv.get_by_text(summary).first.wait_for(timeout=90000)
        btn = conv.get_by_role("button", name=re.compile(r"^Countersign")).last
        expect(btn).to_be_enabled(timeout=30000)
        btn.click()
        dialog = self.page.get_by_role("dialog")
        dialog.get_by_role("button", name="Continue to authenticate").click()
        self.page.wait_for_timeout(500)

    # ------------------------------------------------------------------ journey

    def landing(self) -> str:
        p = self.page
        p.goto(BASE + "/", wait_until="domcontentloaded")
        p.get_by_role("link", name=re.compile("Explore the Countersign Copilot")).first.wait_for(timeout=30000)
        p.get_by_text("All systems operational").first.wait_for(state="attached", timeout=30000)
        p.wait_for_timeout(800)
        self.shot("01_landing")
        assert self.no_overflow(), "horizontal overflow on the landing page"
        assert p.get_by_role("button", name=re.compile("Sign in to demo account")).count() == 0, "demo sign-in button is back"
        return "hero, Explore CTA and live public status"

    def explore_signed_out(self) -> str:
        p = self.page
        p.get_by_role("link", name=re.compile("Explore the Countersign Copilot")).first.click()
        p.wait_for_url(re.compile(r"/signin\?next=(%2F|/)copilot"), timeout=20000)
        p.locator("#signin-email").wait_for(timeout=15000)
        self.shot("02_signin")
        return urlparse(p.url).path + "?" + urlparse(p.url).query

    def signup(self) -> str:
        p = self.page
        p.get_by_role("link", name="Create an account").click()
        p.wait_for_url(re.compile(r"/signup"), timeout=15000)
        p.locator("#signup-name").fill(self.name)
        p.locator("#signup-email").fill(self.email)
        p.locator("#signup-password").fill(self.password)
        self.shot("03_signup")
        with p.expect_response(lambda r: urlparse(r.url).path == "/api/auth/signup", timeout=30000) as info:
            p.get_by_role("button", name=re.compile("Create account")).click()
        assert info.value.status == 201, f"signup returned {info.value.status}"
        me = info.value.json()
        self.account_id = me["account"]["id"]
        self.signed_in = True
        assert re.fullmatch(r"PT[A-Z2-7]{6}", self.account_id), self.account_id
        assert me["onboarding"]["complete"] is False
        p.wait_for_url(re.compile(r"/onboarding"), timeout=20000)
        return f"{self.email} -> paper account {self.account_id}"

    def onboarding_pin(self) -> str:
        p = self.page
        new = p.get_by_label("New 4-digit PIN", exact=True)
        confirm = p.get_by_label("Confirm your PIN", exact=True)
        save = p.get_by_role("button", name=re.compile("Save PIN and continue"))
        new.click()
        p.keyboard.type("1234")
        confirm.click()
        p.keyboard.type("1234")
        p.get_by_text(re.compile("ascending run like 1234")).first.wait_for(timeout=5000)
        expect(save).to_be_disabled()
        self.shot("04_weak_pin")
        # The server refuses it too, whatever the page does.
        st, body = self.api_post("/api/auth/pin", {"pin": "1234"})
        assert st == 422 and body.get("code") == "WEAK_PIN", (st, body)
        for slots in (new, confirm):
            slots.focus()
            for _ in range(4):
                p.keyboard.press("Backspace")
        new.focus()
        p.keyboard.type(self.pin)
        confirm.focus()
        p.keyboard.type(self.pin)
        expect(save).to_be_enabled(timeout=5000)
        save.click()
        p.get_by_role("heading", name="Add a passkey on this device").wait_for(timeout=15000)
        return "1234 refused in the page and by the API (422 WEAK_PIN); chosen PIN saved"

    def onboarding_passkey(self) -> str:
        p = self.page
        p.get_by_role("button", name=re.compile(r"Register passkey")).click()
        p.get_by_role("heading", name="Link an authenticator app").wait_for(timeout=20000)
        st, me = self.api_get("/api/me")
        live = [d for d in me["devices"] if not d["revoked"]]
        assert len(live) == 1 and me["onboarding"]["passkeys"] == 1, me["onboarding"]
        return f"registered '{live[0]['label']}'"

    def onboarding_totp(self) -> str:
        p = self.page
        with p.expect_response(lambda r: urlparse(r.url).path == "/api/auth/totp/setup", timeout=20000) as info:
            p.get_by_role("button", name="Set up authenticator").click()
        secret = info.value.json()["secret"]
        p.get_by_label("Authenticator code", exact=True).wait_for(timeout=10000)
        self.shot("05_totp")
        # Avoid typing a code in the last two seconds of its 30 s window.
        if time.time() % 30 > 27:
            p.wait_for_timeout(3500)
        p.get_by_label("Authenticator code", exact=True).click()
        p.keyboard.type(totp_now(secret))
        p.get_by_role("heading", name="You are ready to countersign").wait_for(timeout=20000)
        self.shot("06_onboarding_done")
        for text in ("Set", "1 registered", "Linked"):
            p.get_by_text(text, exact=True).first.wait_for(timeout=5000)
        return "authenticator linked; summary shows PIN, passkey and authenticator"

    def enter_copilot(self) -> str:
        p = self.page
        # A kill switch left engaged by an earlier session would refuse every trade; release it with this user's PIN.
        st, sec = self.api_get("/api/security/status")
        released = ""
        if st == 200 and sec["kill_switch"]["engaged"]:
            st2, _ = self.api_post("/api/security/kill-switch", {"engaged": False, "pin": self.pin})
            released = f" (released a kill switch left engaged: {st2})"
        p.get_by_role("button", name=re.compile("Enter the Copilot")).click()
        self.wait_copilot()
        return "landed in /copilot" + released

    def header(self) -> str:
        p = self.page
        header = p.locator("header").first
        status = header.get_by_role("button", name=re.compile(r"^System status"))
        expect(status).to_have_attribute("aria-label", "System status: All systems normal", timeout=20000)
        p.wait_for_timeout(600)
        self.shot("07_copilot")
        text = header.inner_text()
        for crowded in ("KILL SWITCH", "Presenter", "Replay", "Broker", "Feed", "Monitor", "Executor", "Live"):
            assert crowded not in text, f"header still shows '{crowded}'"
        buttons = header.get_by_role("button").count()
        assert buttons <= 3, f"{buttons} header buttons (search, status, account expected)"
        hb = header.bounding_box()
        mb = p.locator("main#main").bounding_box()
        assert hb and mb and hb["height"] <= 72, hb
        assert mb["y"] - (hb["y"] + hb["height"]) < 2, "a second row sits between the header and the page"
        assert self.no_overflow(), "horizontal overflow on /copilot"
        return f"one row, {hb['height']:.0f}px, {buttons} controls, status 'All systems normal'"

    def read_only(self) -> str:
        t = time.time()
        self.send("What is my P&L today?")
        answer = self.conversation().get_by_text(re.compile(r"P&L is [-+]?₹")).first
        answer.wait_for(timeout=60000)
        took = time.time() - t
        self.conversation_id = self.page.evaluate("sessionStorage.getItem('cs_conversation_id')")
        self.page.wait_for_timeout(400)
        self.shot("08_read_only")
        return f"{took:.1f}s: {answer.inner_text().strip()[:90]}"

    def passkey_countersign(self) -> str:
        p = self.page
        # Drafts use the deterministic engine so the journey never depends on a hosted model's answer (a model may
        # ask a clarifying question instead of drafting). The countersign path after the draft is identical.
        self.use_engine("Deterministic")
        st, q = self.api_get("/api/market/quote/RELIANCE")
        assert st == 200, q
        price = (q["tick"]["ltp_paise"] + 99) // 100  # whole rupees at or just above the live price (on the tick grid)
        t = time.time()
        before = self.dones()
        self.draft(f"Buy 10 RELIANCE at {price}", re.compile(r"BUY 10 RELIANCE"))
        dialog = p.get_by_role("dialog")
        dialog.get_by_role("tab", name=re.compile("Passkey")).first.click()
        btn = dialog.get_by_role("button", name=re.compile("Authenticate with passkey"))
        expect(btn).to_be_enabled(timeout=15000)
        btn.click()
        self.wait_more_dones(before)
        self.shot("09_passkey_execution")
        p.keyboard.press("Escape")
        return f"RELIANCE at ₹{price}, {time.time() - t:.1f}s"

    def pin_countersign(self) -> str:
        p = self.page
        st, q = self.api_get("/api/market/quote/TCS")
        assert st == 200, q
        price = (q["tick"]["ltp_paise"] + 500) // 100  # whole rupees just above the last price, so it can fill
        t = time.time()
        p.wait_for_timeout(800)
        before = self.dones()
        self.draft(f"Buy 1 TCS at {price}", re.compile(r"BUY 1 TCS"))
        dialog = p.get_by_role("dialog")
        dialog.get_by_role("tab", name=re.compile("PIN")).first.click()
        dialog.get_by_label("App PIN", exact=True).click()
        p.keyboard.type(self.pin)
        # The PIN slots submit on the fourth digit; the button is only a fallback.
        p.wait_for_timeout(500)
        submit = dialog.get_by_role("button", name=re.compile("Countersign with PIN"))
        if submit.count() and submit.first.is_visible() and submit.first.is_enabled():
            submit.first.click()
        self.wait_more_dones(before)
        self.shot("10_pin_execution")
        p.keyboard.press("Escape")
        return f"TCS at ₹{price}, {time.time() - t:.1f}s"

    def security_center(self) -> str:
        p = self.page
        p.wait_for_timeout(500)
        self.account_menu("Security Center")
        p.wait_for_url(re.compile(r"/security"), timeout=20000)
        p.get_by_text("Your account", exact=True).first.wait_for(timeout=20000)
        p.get_by_text(self.email).first.wait_for(timeout=10000)
        p.get_by_text(self.account_id).first.wait_for(timeout=10000)
        p.wait_for_timeout(600)
        self.shot("12_security")
        assert self.no_overflow(), "horizontal overflow on /security"
        return "account panel shows email and paper account"

    def attack_lab(self) -> str:
        p = self.page
        p.goto(BASE + "/attack-lab", wait_until="domcontentloaded")
        btn = p.get_by_role("button", name=re.compile("Run all attacks")).first
        expect(btn).to_be_enabled(timeout=20000)
        t = time.time()
        with p.expect_response(lambda r: urlparse(r.url).path == "/api/attack-lab/run", timeout=120000) as resp:
            btn.click()
        run = resp.value.json()
        statuses = [r["status"] for r in run["results"]]
        unapproved = sum(r["unapproved_orders"] for r in run["results"])
        p.wait_for_timeout(800)
        self.shot("11_attack_lab")
        assert len(statuses) == 9 and all(s == "BLOCKED" for s in statuses) and unapproved == 0, (statuses, unapproved)
        return f"{statuses.count('BLOCKED')}/{len(statuses)} blocked, {unapproved} unapproved, {time.time() - t:.1f}s"

    def kill_switch(self) -> str:
        p = self.page
        p.get_by_role("button", name=re.compile("Engage kill switch")).first.click()
        p.get_by_label("PIN to disengage", exact=True).wait_for(timeout=10000)
        p.get_by_text("Kill switch engaged.").first.wait_for(timeout=10000)
        p.get_by_label("PIN to disengage", exact=True).click()
        p.keyboard.type(self.pin)
        p.get_by_role("button", name=re.compile("Disengage with PIN")).first.click()
        p.get_by_role("button", name=re.compile("Engage kill switch")).first.wait_for(timeout=10000)
        expect(p.get_by_text("Kill switch engaged.")).to_have_count(0, timeout=10000)
        return "red banner while engaged; released with the chosen PIN"

    def sign_out_to_landing(self) -> str:
        self.sign_out()
        self.page.get_by_role("link", name=re.compile("Explore the Countersign Copilot")).first.wait_for(timeout=20000)
        st, _ = self.api_get("/api/me")
        assert st == 401, f"/api/me after sign-out returned {st}"
        return "back on the landing page, session revoked"

    def password_sign_in(self) -> str:
        p = self.page
        # Wait for hydration: text typed into the server-rendered form before React takes over is reset.
        p.goto(BASE + "/signin", wait_until="networkidle")
        p.locator("#signin-email").fill(self.email.upper())
        p.locator("#signin-password").fill(self.password)
        with p.expect_response(lambda r: urlparse(r.url).path == "/api/auth/login", timeout=30000) as info:
            p.get_by_role("button", name="Sign in", exact=True).click()
        assert info.value.status == 200, info.value.status
        self.signed_in = True
        self.wait_copilot()
        # Signed in, the landing page's Explore goes straight into the Copilot.
        p.goto(BASE + "/", wait_until="domcontentloaded")
        explore = p.get_by_role("link", name=re.compile("Explore the Countersign Copilot")).first
        expect(explore).to_have_attribute("href", "/copilot", timeout=20000)
        explore.click()
        self.wait_copilot()
        return "email (any case) and password, then Explore opens /copilot directly"

    def passkey_sign_in(self) -> str:
        p = self.page
        p.goto(BASE + "/signin", wait_until="networkidle")
        with p.expect_response(lambda r: urlparse(r.url).path == "/api/auth/passkey/login/finish", timeout=30000) as info:
            p.get_by_role("button", name=re.compile("Sign in with a passkey")).click()
        assert info.value.status == 200, info.value.status
        self.signed_in = True
        self.wait_copilot()
        st, me = self.api_get("/api/me")
        assert st == 200 and me["email"] == self.email, (st, me.get("email") if isinstance(me, dict) else me)
        return "discoverable passkey, no email typed"

    # ------------------------------------------------------------------ 021 journey (--broker 021)

    def sign_in_021(self) -> str:
        p = self.page
        p.goto(BASE + "/signin", wait_until="networkidle")
        p.locator("#signin-email").fill(self.email)
        p.locator("#signin-password").fill(self.password)
        with p.expect_response(lambda r: urlparse(r.url).path == "/api/auth/login", timeout=30000) as info:
            p.get_by_role("button", name="Sign in", exact=True).click()
        assert info.value.status == 200, info.value.status
        self.signed_in = True
        self.wait_copilot()
        st, me = self.api_get("/api/me")
        acct = (me or {}).get("account") or {}
        assert st == 200 and acct.get("kind") == "021", f"active account is {acct.get('kind')!r}, not the 021 sandbox account"
        st, accts = self.api_get("/api/broker/accounts")
        live = [a for a in accts if a["kind"] == "021"] if st == 200 else []
        assert live and live[0]["status"]["connected"], f"021 account not connected: {live}"
        self.shot("21_021_signed_in")
        return f"landed on '{acct.get('label')}' (gateway connected, token until {live[0]['status']['token_expires_at']})"

    def read_021(self) -> str:
        conv = self.conversation()
        out = []
        # Patterns match only the copilot's answers (never the question bubble), and each answer finishes streaming
        # before the next question is sent.
        for q, pattern in (
            ("What's my P&L today?", r"P&L is [-+]?₹"),
            ("What did I pay on average for INFY?", r"You paid ₹[\d,.]+ on average for INFY"),
            ("Show NIFTY options near the money for this expiry", r"NIFTY spot is ₹|option chain is not available"),
        ):
            t = time.time()
            self.send(q)
            conv.get_by_text(re.compile(pattern)).first.wait_for(timeout=90000)
            expect(self.page.get_by_label("Message the copilot")).to_be_editable(timeout=30000)
            self.page.wait_for_timeout(1200)
            out.append(f"{q} {time.time() - t:.1f}s")
        self.page.wait_for_timeout(400)
        self.shot("22_021_read")
        return "; ".join(out)

    def already_true_021(self) -> str:
        conv = self.conversation()
        self.use_engine("Deterministic")
        self.send("If Reliance drops below 2800, buy 10")
        conv.get_by_text(re.compile(r"already true")).first.wait_for(timeout=60000)
        opt_now = conv.get_by_role("button", name=re.compile(r"^Buy 10 RELIANCE now at limit")).first
        opt_trig = conv.get_by_role("button", name=re.compile(r"^If RELIANCE drops below")).first
        opt_now.wait_for(timeout=10000)
        opt_trig.wait_for(timeout=10000)
        self.shot("23_021_already_true")
        return f"asked instead of arming; options '{opt_now.inner_text().strip()}' / '{opt_trig.inner_text().strip()}'"

    def _orders_021(self) -> list[dict]:
        st, rows = self.api_get("/api/account/orders?status=all")
        return rows if st == 200 and isinstance(rows, list) else []

    def _pin_in_dialog(self) -> None:
        p = self.page
        dialog = p.get_by_role("dialog")
        dialog.get_by_role("tab", name=re.compile("PIN")).first.click()
        dialog.get_by_label("App PIN", exact=True).click()
        p.keyboard.type(self.pin)
        # The PIN slots submit on the fourth digit; the button is only a fallback.
        p.wait_for_timeout(500)
        submit = dialog.get_by_role("button", name=re.compile("Countersign with PIN"))
        if submit.count() and submit.first.is_visible() and submit.first.is_enabled():
            submit.first.click()

    def order_and_cancel_021(self) -> str:
        p = self.page
        conv = self.conversation()
        st, q = self.api_get("/api/market/quote/ITC")
        assert st == 200, q
        limit = (q["tick"]["ltp_paise"] * 92 // 100) // 5 * 5  # about 8% below the market, on the 5 paise grid
        known = {o["broker_order_id"] for o in self._orders_021()}
        text = f"Buy 1 ITC at {limit // 100}.{limit % 100:02d} delivery"
        before = self.dones()
        self.draft(text, re.compile(r"BUY 1 ITC"))
        self._pin_in_dialog()
        self.wait_more_dones(before, timeout=90)
        p.keyboard.press("Escape")
        order = None
        end = time.time() + 30
        while time.time() < end and order is None:
            order = next((o for o in self._orders_021() if o["broker_order_id"] not in known and o.get("symbol") == "ITC"), None)
            p.wait_for_timeout(500)
        assert order is not None, "the order did not appear in the 021 order book"
        oid = order["broker_order_id"]
        assert order.get("price_021_paise") == limit and order.get("origin") == "countersign", {k: order.get(k) for k in ("price_021_paise", "origin", "status_021")}
        self.shot("24_021_placed")
        exact = f"{text}: order {oid} in 021's book at exactly ₹{limit / 100:.2f}"
        if order["status"] not in ("OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED"):
            return f"{exact}; 021 filled it ({order.get('status_021')}) before a cancellation could be sent"
        self.send("Cancel my ITC order")
        conv.get_by_text(re.compile(r"CANCEL order|no open order|Which order")).first.wait_for(timeout=60000)
        if conv.get_by_text(re.compile(r"no open order")).count():
            return f"{exact}; 021 filled it before the cancellation was drafted"
        btn = conv.get_by_role("button", name=re.compile(r"^Countersign")).last
        expect(btn).to_be_enabled(timeout=30000)
        before = self.dones()
        btn.click()
        p.get_by_role("dialog").get_by_role("button", name="Continue to authenticate").click()
        p.wait_for_timeout(500)
        self._pin_in_dialog()
        self.wait_more_dones(before, timeout=90)
        p.keyboard.press("Escape")
        final = order
        end = time.time() + 30
        while time.time() < end:
            final = next((o for o in self._orders_021() if o["broker_order_id"] == oid), final)
            if final["status"] in ("CANCELLED", "COMPLETE"):
                break
            p.wait_for_timeout(1000)
        self.shot("25_021_cancelled")
        assert final["status"] in ("CANCELLED", "COMPLETE"), final.get("status_021")
        return f"{exact}; cancel countersigned with the PIN; 021 now reports {final.get('status_021')}"

    def guard(self) -> str:
        p = self.page
        p.goto(BASE + "/copilot", wait_until="domcontentloaded")
        p.wait_for_url(re.compile(r"/signin\?next=(%2F|/)copilot"), timeout=20000)
        p.locator("#signin-email").wait_for(timeout=15000)
        return urlparse(p.url).path + "?" + urlparse(p.url).query


def api_client() -> httpx.Client:
    return httpx.Client(base_url=API, timeout=60, headers={"Origin": BASE})


def isolation_and_seed(j: Journey) -> None:
    """A second, brand-new user over the API: seeded paper account, and none of the first user's data."""
    c = api_client()
    other = f"e2e.other.{secrets.token_hex(4)}@example.test"
    r = c.post("/api/auth/signup", json={"display_name": "Second E2E", "email": other, "password": f"other-{secrets.token_hex(6)}"})
    if not check("second user signs up", r.status_code == 201, f"{r.status_code}"):
        return
    me = r.json()
    h = {"X-CSRF-Token": c.cookies.get("cs_csrf") or ""}
    summary = c.get("/api/account/summary").json()
    holdings = {x["instrument"]["symbol"]: x["quantity"] for x in c.get("/api/account/holdings").json()}
    positions = {(x["instrument"]["symbol"], x["product"]): x["quantity"] for x in c.get("/api/account/positions").json()}
    seeded = holdings == SEED_HOLDINGS and positions == SEED_POSITIONS and summary["cash_paise"] == SEED_CASH_PAISE and summary["open_orders_count"] == 0
    check("new paper account is seeded like the demo", seeded, f"{me['account']['id']}: {len(holdings)} holdings, {len(positions)} position, cash {rupees(summary['cash_paise'])}")
    check("accounts are distinct", me["account"]["id"] not in (j.account_id, "DEMO1"), f"{j.account_id} vs {me['account']['id']}")
    if not j.proposals:
        check("second user cannot see the first user's data", True, "skipped: the first user drafted no proposals in this run")
        c.post("/api/auth/logout", headers=h)
        return
    listed = {p["id"] for p in c.get("/api/proposals").json()}
    direct = [c.get(f"/api/proposals/{pid}").status_code for pid in j.proposals]
    approve = [c.post(f"/api/proposals/{pid}/approval/start", json={"method": "pin"}, headers=h).status_code for pid in j.proposals[:1]]
    orders = c.get("/api/account/orders?status=all").json()
    chat = c.post("/api/chat", json={"message": "hello", "conversation_id": j.conversation_id}, headers=h).status_code if j.conversation_id else 404
    ok = bool(j.proposals) and not (listed & set(j.proposals)) and all(s == 404 for s in direct + approve) and orders == [] and chat == 404
    check("second user cannot see the first user's data", ok, f"{len(j.proposals)} proposals: list {len(listed & set(j.proposals))}, GET {direct}, approval {approve}; orders {len(orders)}; chat {chat}")
    c.post("/api/auth/logout", headers=h)


def network_guards() -> None:
    """The credential-free demo sign-in is for scripts calling the API directly, never through the web server."""
    r = api_client().post("/api/auth/demo-login", json={})
    check("demo sign-in refused through the web server", r.status_code == 403 and "cs_session" not in r.cookies, f"{r.status_code}")


def cleanup(j: Journey) -> int:
    """Revokes the passkey the run registered (the virtual authenticator disappears with the browser) and releases
    the kill switch if a failed step left it engaged."""
    c = api_client()
    r = c.post("/api/auth/login", json={"email": j.email, "password": j.password})
    if r.status_code != 200:
        return 0
    h = {"X-CSRF-Token": c.cookies.get("cs_csrf") or ""}
    if c.get("/api/security/status").json()["kill_switch"]["engaged"]:
        c.post("/api/security/kill-switch", json={"engaged": False, "pin": j.pin}, headers=h)
    devices = [d for d in c.get("/api/me").json().get("devices", []) if not d["revoked"]]
    for d in devices:
        c.delete(f"/api/auth/devices/{d['id']}", headers=h)
    c.post("/api/auth/logout", headers=h)
    return len(devices)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--device", help='Playwright device descriptor run in Chromium, for example "Pixel 7" or "iPhone 15"')
    ap.add_argument("--base", help="web origin to test, for example https://countersign.example.com (default http://localhost:3000)")
    ap.add_argument("--broker", choices=("paper", "021"), default="paper", help="paper (default): a new user on a paper account; 021: an existing user linked to the 021 sandbox")
    ap.add_argument("--login-file", help="--broker 021: a file with 'Email: ...', 'Password: ...' and 'App PIN (countersign): ....' lines (never printed)")
    args = ap.parse_args()
    if args.broker == "021" and not args.login_file:
        print("--broker 021 needs --login-file")
        return 2
    if args.base:
        global BASE
        BASE = args.base.rstrip("/")
    SHOTS.mkdir(parents=True, exist_ok=True)
    prefix = re.sub(r"[^a-z0-9]+", "-", args.device.lower()).strip("-") if args.device else "desktop"
    revoked = 0
    with sync_playwright() as p:
        if args.device:
            if args.device not in p.devices:
                print(f"unknown device {args.device!r}; try 'Pixel 7' or 'iPhone 15'")
                return 2
            opts = {k: v for k, v in p.devices[args.device].items() if k != "default_browser_type"}
        else:
            opts = {"viewport": {"width": 1440, "height": 900}}
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(**opts)
        page = ctx.new_page()
        cdp = ctx.new_cdp_session(page)
        cdp.send("WebAuthn.enable")
        cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {"protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})
        j = Journey(page, ctx, prefix + ("-021" if args.broker == "021" else ""))
        if args.broker == "021":
            raw = Path(args.login_file).read_text(encoding="utf-8")
            j.email = re.search(r"Email: (\S+)", raw).group(1)
            j.password = re.search(r"Password: (\S+)", raw).group(1)
            j.pin = re.search(r"PIN \(countersign\): (\d{4})", raw).group(1)
        print(f"device: {args.device or 'desktop 1440x900'}; broker {args.broker}" + (f"; user {j.email}" if args.broker == "paper" else ""), flush=True)

        steps = [
            ("landing page", j.landing, True),
            ("Explore while signed out -> sign in", j.explore_signed_out, True),
            ("sign up", j.signup, True),
            ("onboarding: PIN (weak refused, chosen saved)", j.onboarding_pin, True),
            ("onboarding: passkey", j.onboarding_passkey, True),
            ("onboarding: authenticator app", j.onboarding_totp, False),
            ("enter the Copilot", j.enter_copilot, True),
            ("decluttered header", j.header, False),
            ("read-only question", j.read_only, False),
            ("passkey countersign -> broker ack", j.passkey_countersign, False),
            ("PIN countersign -> broker ack", j.pin_countersign, False),
            ("Attack Lab from the UI", j.attack_lab, False),
            ("Security Center", j.security_center, False),
            ("kill switch engage and release", j.kill_switch, False),
            ("sign out", j.sign_out_to_landing, True),
            ("sign in with email and password", j.password_sign_in, True),
            ("sign out again", j.sign_out_to_landing, True),
            ("sign in with the passkey", j.passkey_sign_in, True),
            ("sign out a third time", j.sign_out_to_landing, True),
            ("signed-out /copilot redirects to sign-in", j.guard, False),
        ]
        if args.broker == "021":
            steps = [
                ("landing page", j.landing, True),
                ("sign in as a linked 021 owner", j.sign_in_021, True),
                ("decluttered header", j.header, False),
                ("L1 questions on the live 021 account", j.read_021, False),
                ("official standing-instruction example asks instead of arming", j.already_true_021, False),
                ("1-share order on 021 with the PIN, then cancel via the copilot", j.order_and_cancel_021, False),
                ("sign out", j.sign_out_to_landing, True),
                ("signed-out /copilot redirects to sign-in", j.guard, False),
            ]
        for name, fn, critical in steps:
            try:
                check(name, True, fn() or "")
            except Exception as e:  # noqa: BLE001
                j.shot("error_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"))
                check(name, False, str(e).splitlines()[0][:300] if str(e) else type(e).__name__)
                if critical:
                    break
        browser.close()

    if j.account_id:
        isolation_and_seed(j)
        revoked = cleanup(j)
    network_guards()
    check("no page errors", not j.errors, "; ".join(j.errors[:3]))
    check("no unexpected API errors", not j.failed_calls, "; ".join(j.failed_calls[:6]))
    print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} passed (revoked {revoked} passkey(s); screenshots in .logs/e2e/{prefix}_*.png)")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
