"""
INNOVATRIX — HTTPS bridge for phone sensor nodes.

iOS (and modern Android) only expose motion sensors to pages served over HTTPS.
This tiny proxy terminates TLS on port 8443 and forwards the raw bytes to the
real INNOVATRIX server on http://127.0.0.1:8000 — so the phone gets a secure
origin while the backend keeps ONE process and ONE shared state (zones, alerts,
WebSocket). HTTP, JSON APIs and WebSockets all pass straight through.

Run:  py tls_proxy.py     (needs certs/cert.pem + certs/key.pem alongside)
Phone: https://<laptop-ip>:8443/sensor.html?zone=F
       (accept the one-time certificate warning — it's your own self-signed cert)
"""
import asyncio
import os
import ssl

LISTEN_PORT = 8443
TARGET_HOST, TARGET_PORT = "127.0.0.1", 8000


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle(client_r, client_w):
    try:
        server_r, server_w = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except Exception:
        client_w.close()
        return
    await asyncio.gather(_pipe(client_r, server_w), _pipe(server_r, client_w))


async def main():
    base = os.path.dirname(os.path.abspath(__file__))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        os.path.join(base, "certs", "cert.pem"),
        os.path.join(base, "certs", "key.pem"),
    )
    server = await asyncio.start_server(_handle, "0.0.0.0", LISTEN_PORT, ssl=ctx)
    print(f"[tls-bridge] https://0.0.0.0:{LISTEN_PORT}  ->  http://{TARGET_HOST}:{TARGET_PORT}")
    print("[tls-bridge] phones can now use motion sensors via the https:// link")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
