#!/usr/bin/python3
"""Chrome native messaging host and local transport for Promptr."""

import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import uuid


HOST_NAME = "com.toxdes.promptr_gemini_web"
MAX_MESSAGE = 1024 * 1024
HEADER = struct.Struct("=I")
BROWSERS = ("google-chrome", "chromium", "google-chrome-for-testing")


class BridgeError(Exception):
    pass


def socket_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not Path(runtime).is_absolute():
        raise BridgeError("Gemini Web requires XDG_RUNTIME_DIR in your desktop session.")
    return Path(runtime) / "promptr-gemini-web" / "chrome.sock"


def frame(message):
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > MAX_MESSAGE:
        raise BridgeError("Gemini Web message exceeds the 1 MiB Chrome bridge limit.")
    return HEADER.pack(len(body)) + body


async def read_message(reader):
    size, = HEADER.unpack(await reader.readexactly(HEADER.size))
    if not 0 < size <= MAX_MESSAGE:
        raise BridgeError("Invalid Gemini Web bridge message size.")
    value = json.loads(await reader.readexactly(size))
    if not isinstance(value, dict):
        raise BridgeError("Invalid Gemini Web bridge message.")
    return value


async def send_request(operation, *, timeout, **params):
    payload = frame({"operation": operation, **params})
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(socket_path())
            try:
                writer.write(payload)
                await writer.drain()
                response = await read_message(reader)
            finally:
                writer.close()
    except TimeoutError:
        raise BridgeError("The Gemini Web Chrome extension timed out.") from None
    except (OSError, asyncio.IncompleteReadError, ValueError):
        raise BridgeError("Open Chrome and click the Promptr Gemini Web extension to connect.") from None
    if isinstance(response.get("error"), str):
        raise BridgeError(response["error"])
    return response


def send_to_chrome(message):
    sys.stdout.buffer.write(frame(message))
    sys.stdout.buffer.flush()


class NativeHost:
    def __init__(self):
        self.pending = {}
        self.clients = set()

    async def client(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        try:
            request = await asyncio.wait_for(read_message(reader), timeout=5)
            operation = request.get("operation")
            if operation == "ping":
                response = {"ready": True}
            elif operation in ("models", "submit"):
                response = await self.forward(request, reader)
            else:
                response = {"error": "Invalid Gemini Web bridge operation."}
            if response is not None:
                writer.write(frame(response))
                await writer.drain()
        except BridgeError as exc:
            writer.write(frame({"error": str(exc)}))
        except (OSError, asyncio.IncompleteReadError, ValueError, TimeoutError):
            pass
        finally:
            writer.close()
            self.clients.discard(task)

    async def forward(self, request, reader):
        request_id = uuid.uuid4().hex
        response = asyncio.get_running_loop().create_future()
        self.pending[request_id] = response
        disconnected = asyncio.create_task(reader.read(1))
        try:
            send_to_chrome({**request, "id": request_id})
            done, _ = await asyncio.wait(
                (response, disconnected), return_when=asyncio.FIRST_COMPLETED
            )
            if response in done:
                return response.result()
            send_to_chrome({"operation": "cancel", "id": request_id})
            return None
        finally:
            disconnected.cancel()
            self.pending.pop(request_id)

    async def serve(self, path):
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        pipe, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
        )
        try:
            async with await asyncio.start_unix_server(self.client, path=path):
                send_to_chrome({"ready": True})
                while True:
                    message = await read_message(reader)
                    request_id = message.get("id")
                    if isinstance(request_id, str):
                        future = self.pending.get(request_id)
                        if future is not None and not future.done():
                            future.set_result(message)
        except asyncio.IncompleteReadError:
            pass
        finally:
            pipe.close()
            clients = list(self.clients)
            for task in clients:
                task.cancel()
            await asyncio.gather(*clients, return_exceptions=True)


def run_host():
    path = socket_path()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    directory = path.parent.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or directory.st_mode & 0o077):
        raise BridgeError("The Gemini Web runtime directory must be private to your user.")
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("Gemini Web is already connected in another Chrome profile.") from None
        if not path.is_symlink() and path.is_socket():
            path.unlink()
        if path.exists() or path.is_symlink():
            raise BridgeError("The Gemini Web socket path is occupied.")
        try:
            asyncio.run(NativeHost().serve(path))
        finally:
            if path.is_socket():
                path.unlink()
    finally:
        os.close(fd)


def register():
    parser = argparse.ArgumentParser(description="Register the local Gemini Web native messaging host.")
    parser.add_argument("--register", metavar="EXTENSION_ID", required=True)
    parser.add_argument("--browser", choices=BROWSERS, default=BROWSERS[0])
    args = parser.parse_args()
    if not re.fullmatch("[a-p]{32}", args.register):
        parser.error("Copy the 32-character extension ID from chrome://extensions.")
    directories = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    manifest = directories / args.browser / "NativeMessagingHosts" / f"{HOST_NAME}.json"
    payload = {
        "name": HOST_NAME,
        "description": "Promptr Gemini Web",
        "path": str(Path(__file__).resolve()),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{args.register}/"],
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with manifest.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
    except FileExistsError:
        if json.loads(manifest.read_text(encoding="utf-8")) != payload:
            raise BridgeError(f"An existing registration has different settings at {manifest}.") from None
    print(manifest)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1].startswith("--"):
            register()
        else:
            run_host()
    except (BridgeError, OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
