#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.65.2", "markdownify>=1.2,<2"]
# ///

"""Promptr protocol adapter for Gemini web chat."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from markdownify import markdownify
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from native_host import BridgeError, send_request


GEMINI_ORIGIN = "https://gemini.google.com"
MAX_FRAME = 64 * 1024 * 1024
DELTA_CHARS = 16 * 1024


def send(**message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def error(rpc_id, code, message):
    send(id=rpc_id, error={"code": code, "message": message})


def request_ids(params):
    return {key: params.get(key) for key in ("thread_id", "request_id")}


def make_prompt(params):
    messages = params.get("messages")
    if not isinstance(messages, list) or not messages or any(
        not isinstance(message, dict)
        or message.get("role") not in ("user", "assistant", "system")
        or not isinstance(message.get("content"), str)
        for message in messages
    ):
        raise ValueError("Expected an ordered list of conversation messages.")
    current = messages[-1]
    if current["role"] != "user" or not current["content"].strip():
        raise ValueError("Expected a nonempty final user message.")
    agent = params.get("agent")
    if agent is not None and (
        not isinstance(agent, dict) or not isinstance(agent.get("content"), str)
    ):
        raise ValueError("Expected the agent's Markdown content.")
    if params.get("connection_test"):
        return "Reply with exactly pong."
    if len(messages) == 1 and not agent:
        return current["content"]
    context = {"history": messages[:-1]}
    if agent:
        context["agent_instructions"] = agent["content"]
    return (
        "Use this conversation history and any agent instructions as context "
        "when answering the current user message.\n\n"
        + json.dumps(context, ensure_ascii=False)
        + "\n\nCurrent user message:\n"
        + current["content"]
    )


async def request_answer(prompt, connection_test):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel="chromium", chromium_sandbox=True, timeout=8000
        )
        try:
            context = await browser.new_context(locale="en-US")
            await context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin=GEMINI_ORIGIN
            )
            page = await context.new_page()
            page.set_default_timeout(5000)
            await page.goto(
                GEMINI_ORIGIN + "/app?hl=en",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            editor = page.get_by_role("textbox", name="Enter a prompt for Gemini")
            await editor.click()
            await page.keyboard.insert_text(prompt)
            await page.get_by_role("button", name="Send message", exact=True).click()
            copy = page.locator("model-response").last.get_by_role(
                "button", name="Copy", exact=True
            )
            await copy.click(timeout=25000 if connection_test else 120000)
            await page.wait_for_function(
                "async () => (await navigator.clipboard.readText()).trim().length > 0"
            )
            return await page.evaluate("navigator.clipboard.readText()")
        finally:
            await browser.close()


class Provider:
    def __init__(self):
        self.session = None
        self.pending = None

    async def handle(self, request):
        if not isinstance(request, dict):
            error(None, -32600, "Invalid JSON-RPC request.")
            return
        rpc_id = request.get("id")
        method = request.get("method")
        if (
            request.get("jsonrpc") != "2.0"
            or type(rpc_id) is not int
            or not 0 <= rpc_id <= 2147483647
            or not isinstance(method, str)
        ):
            error(None, -32600, "Invalid JSON-RPC request.")
            return
        params = request.get("params", {})
        if not isinstance(params, dict):
            error(rpc_id, -32602, "Expected a parameters object.")
            return

        if method == "provider/initialize":
            await self.initialize(rpc_id, params)
        elif method == "provider/submit":
            self.submit(rpc_id, params)
        elif method == "provider/models":
            self.models(rpc_id, params)
        elif method == "provider/cancel":
            send(id=rpc_id, result={"ok": True})
            if self.pending and self.pending[1] == request_ids(params):
                self.cancel()
        else:
            error(rpc_id, -32601, "Unsupported provider method.")

    async def initialize(self, rpc_id, params):
        if params.get("protocol_version") != "2.0":
            error(rpc_id, -32001, "Gemini Web requires provider protocol 2.0.")
            return
        config = params.get("config", {})
        session = config.get("session", "guest") if isinstance(config, dict) else None
        try:
            if session == "signed-in":
                await send_request("ping", timeout=3)
            elif session == "guest":
                async with async_playwright() as playwright:
                    if not Path(playwright.chromium.executable_path).is_file():
                        raise BridgeError("Run gemini_web.py --install-browser to install Chromium.")
            else:
                raise BridgeError("Choose guest or signed-in for Browser session.")
        except (BridgeError, BrowserError) as exc:
            error(rpc_id, -32010, str(exc) if isinstance(exc, BridgeError)
                  else "Chromium couldn't initialize. Check the Gemini Web browser installation.")
            return
        self.session = session
        send(id=rpc_id, result={
            "protocol_version": "2.0",
            "capabilities": {"streaming": False, "models": True},
        })

    def available(self, rpc_id):
        if self.session is None:
            error(rpc_id, -32010, "Initialize the provider before submitting.")
            return False
        if self.pending:
            error(rpc_id, -32020, "Gemini Web already has a pending request.")
            return False
        return True

    def models(self, rpc_id, params):
        if not self.available(rpc_id):
            return
        if self.session == "guest":
            send(id=rpc_id, result={"models": ["auto"]})
            return
        task = asyncio.create_task(send_request("models", timeout=45))
        self.pending = task, request_ids(params)
        task.add_done_callback(lambda done: self.finish_models(rpc_id, done))

    def finish_models(self, rpc_id, task):
        self.pending = None
        try:
            models = task.result().get("models")
        except asyncio.CancelledError:
            error(rpc_id, -32000, "Model discovery cancelled.")
            return
        except BridgeError as exc:
            error(rpc_id, -32000, str(exc))
            return
        if not isinstance(models, list) or not models or not all(
            isinstance(model, str) and model and len(model.encode()) <= 1024 for model in models
        ):
            error(rpc_id, -32000, "Gemini Web returned no usable models.")
            return
        send(id=rpc_id, result={"models": models})

    def submit(self, rpc_id, params):
        if not self.available(rpc_id):
            return
        ids = request_ids(params)
        if any(
            not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256
            for value in ids.values()
        ):
            error(rpc_id, -32602, "Expected valid thread and request IDs.")
            return
        try:
            prompt = make_prompt(params)
            model = params.get("model") or "auto"
            if not isinstance(model, str) or len(model.encode()) > 1024:
                raise ValueError("Expected a valid Gemini model name.")
            if self.session == "guest" and model != "auto":
                raise ValueError("Gemini Web guest sessions use the auto model.")
        except ValueError as exc:
            error(rpc_id, -32602, str(exc))
            return
        connection_test = bool(params.get("connection_test"))
        task = asyncio.create_task(self.answer(prompt, model, connection_test))
        self.pending = task, ids
        task.add_done_callback(lambda done: self.finish(rpc_id, ids, done))

    async def answer(self, prompt, model, connection_test):
        if self.session == "guest":
            return await request_answer(prompt, connection_test)
        result = await send_request(
            "submit", timeout=50 if connection_test else 155,
            model=model, prompt=prompt, connection_test=connection_test,
        )
        html = result.get("html")
        if not isinstance(html, str):
            raise BridgeError("Gemini Web returned no answer.")
        return markdownify(html, heading_style="ATX")

    def finish(self, rpc_id, ids, task):
        self.pending = None
        try:
            answer = task.result()
        except asyncio.CancelledError:
            send(id=rpc_id, result={**ids, "status": "cancelled"})
            return
        except BrowserTimeout:
            error(rpc_id, -32000, "Gemini Web timed out. Check signed-out chat at gemini.google.com.")
            return
        except BrowserError:
            error(rpc_id, -32000, "Chromium couldn't complete the Gemini Web request.")
            return
        except BridgeError as exc:
            error(rpc_id, -32000, str(exc))
            return
        if not answer.strip():
            error(rpc_id, -32000, "Gemini Web returned an empty response.")
            return
        for offset in range(0, len(answer), DELTA_CHARS):
            send(method="provider/event", params={
                **ids, "type": "delta", "output": answer[offset:offset + DELTA_CHARS]
            })
        send(id=rpc_id, result={**ids, "status": "completed"})

    def cancel(self):
        task, _ = self.pending
        if not task.cancelling():
            task.cancel()
        return task


async def serve():
    stdin = sys.stdin.buffer
    # /dev/null is already at EOF and cannot be watched by the event loop.
    if os.path.samestat(os.fstat(stdin.fileno()), os.stat(os.devnull)):
        return

    provider = Provider()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_FRAME)
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), stdin
    )
    try:
        while True:
            try:
                line = await reader.readline()
            except ValueError:
                error(None, -32600, "Provider request exceeded the frame limit.")
                break
            if not line:
                break
            try:
                request = json.loads(line)
            except ValueError:
                error(None, -32700, "Invalid JSON.")
                continue
            await provider.handle(request)
    finally:
        transport.close()
        if provider.pending:
            await asyncio.gather(provider.cancel(), return_exceptions=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--install-browser"]:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--no-shell", "chromium"],
            env={**os.environ, "PLAYWRIGHT_SKIP_BROWSER_GC": "1"},
            check=False,
        )
        sys.exit(result.returncode)
    asyncio.run(serve())
