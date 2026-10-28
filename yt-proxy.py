#!/usr/bin/env python3
"""YouTube DPI bypass proxy for a home LAN, no VPN: traffic still goes out directly.

One port speaks SOCKS5 and HTTP proxy, and serves a PAC file at http://<host>:<port>/proxy.pac.
The TLS ClientHello is cut into several TLS records and TCP segments so the ISP's DPI
can't read the site name (SNI).

YT_STRATEGY: none     - plain relay
             split    - ClientHello cut after byte 1 and in the middle of the SNI
             disorder - split, and the first piece is sent with TTL=1: it dies at the LAN
                        router, the kernel resends it later, DPI sees the pieces out of order
install.sh tries all three and keeps the one that works best on this network."""
import asyncio, ipaddress, os, socket, struct, time

PORT = int(os.environ.get('YT_PORT', 1080))
STRATEGY = os.environ.get('YT_STRATEGY', 'disorder')
PAC = '''function FindProxyForURL(url, host) {
  var d = ["youtube.com", "youtu.be", "googlevideo.com", "ytimg.com", "ggpht.com",
           "youtube-nocookie.com", "googleapis.com"];
  for (var i = 0; i < d.length; i++)
    if (host == d[i] || dnsDomainIs(host, "." + d[i])) return "PROXY HOST; DIRECT";
  return "DIRECT";
}
'''
ERRORS = (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
          OSError, ValueError, IndexError)


def sni_offset(rec):
    """Offset of the SNI host name inside a ClientHello record, or None."""
    try:
        p = 5 + 4 + 2 + 32                                # record hdr, hs hdr, version, random
        p += 1 + rec[p]                                   # session id
        p += 2 + int.from_bytes(rec[p:p + 2], 'big')      # cipher suites
        p += 1 + rec[p]                                   # compression methods
        end = p + 2 + int.from_bytes(rec[p:p + 2], 'big')
        p += 2
        while p + 4 <= end:
            t, l = struct.unpack('>HH', rec[p:p + 4])
            if t == 0:
                return p + 4 + 5                          # list len, name type, name len
            p += 4 + l
    except (IndexError, struct.error):
        pass
    return None


def split_hello(rec):
    """ClientHello record -> list of smaller records (cut after byte 1 and mid-SNI)."""
    body, hdr = rec[5:], rec[:3]
    cuts = [1]
    sni = sni_offset(rec)
    if sni:
        cuts.append(sni - 5 + 3)
    cuts = sorted(c for c in set(cuts) if 0 < c < len(body))
    parts = [body[a:b] for a, b in zip([0] + cuts, cuts + [len(body)])]
    return [hdr + struct.pack('>H', len(x)) + x for x in parts]


async def dial(loop, info):
    fam, typ, proto, _, addr = info
    sock = socket.socket(fam, typ, proto)
    sock.setblocking(False)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        await loop.sock_connect(sock, addr)
        return sock
    except BaseException:
        sock.close()
        raise


def close_late(task):
    if not task.cancelled() and task.exception() is None:
        task.result().close()


async def connect(host, port, tries=6, timeout=8):
    """The ISP silently drops a share of new connections to some IPs (Telegram, Google).
    A new attempt (new source port, next address) starts every 0.5 s and the first one that
    connects wins; normal sites answer long before the second attempt."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    infos.sort(key=lambda i: i[0] != socket.AF_INET)      # IPv4 first: many home servers have no IPv6 route
    todo = [infos[k % min(3, len(infos))] for k in range(tries)]
    pending, err, end = set(), None, loop.time() + timeout
    try:
        while todo or pending:
            if todo:
                pending.add(asyncio.ensure_future(dial(loop, todo.pop(0))))
            wait = 0.5 if todo else end - loop.time()
            if wait <= 0:
                break
            done, pending = await asyncio.wait(pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            socks = [t.result() for t in done if t.exception() is None]
            for s in socks[1:]:
                s.close()
            if socks:
                return socks[0]
            err = next((t.exception() for t in done), err)
        raise err or TimeoutError('cannot connect to %s:%s' % (host, port))
    finally:
        for t in pending:
            t.cancel()
            t.add_done_callback(close_late)               # it may have connected just before the cancel


async def send_hello(sock, recs):
    """Every record goes out as its own TCP segment; with 'disorder' the first one with TTL=1."""
    loop = asyncio.get_running_loop()
    lvl, opt = ((socket.IPPROTO_IP, socket.IP_TTL) if sock.family == socket.AF_INET
                else (socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS))
    ttl = sock.getsockopt(lvl, opt)
    for i, x in enumerate(recs):
        low = i == 0 and STRATEGY == 'disorder'
        if low:
            sock.setsockopt(lvl, opt, 1)
        await loop.sock_sendall(sock, x)
        if low:
            await asyncio.sleep(0.01)
            sock.setsockopt(lvl, opt, ttl)


async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except OSError:
        pass
    finally:
        w.close()


async def relay(cr, cw, host, port, first=None):
    sock = await connect(host, port)
    try:
        if first is None:
            try:
                first = await asyncio.wait_for(cr.read(65536), 3)
            except asyncio.TimeoutError:                  # server-speaks-first protocols (SSH, SMTP)
                first = b''
            if STRATEGY != 'none' and first[:1] == b'\x16' and len(first) > 5:
                need = 5 + int.from_bytes(first[3:5], 'big')
                while len(first) < need and (more := await cr.read(65536)):
                    first += more
                await send_hello(sock, split_hello(first[:need]))
                first = first[need:]
        rr, rw = await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise
    rw.write(first)
    await rw.drain()
    await asyncio.gather(pipe(cr, rw), pipe(rr, cw))


async def handle(cr, cw):
    peer = cw.get_extra_info('peername')[0]
    try:
        if not ipaddress.ip_address(peer).is_private:     # LAN only, never an open proxy
            return cw.close()
        b = await cr.readexactly(1)
        if b == b'\x05':                                  # SOCKS5, no auth
            await cr.readexactly((await cr.readexactly(1))[0])
            cw.write(b'\x05\x00')
            _, cmd, _, atyp = await cr.readexactly(4)
            if atyp == 1:
                host = socket.inet_ntoa(await cr.readexactly(4))
            elif atyp == 3:
                host = (await cr.readexactly((await cr.readexactly(1))[0])).decode()
            else:
                host = socket.inet_ntop(socket.AF_INET6, await cr.readexactly(16))
            port = struct.unpack('>H', await cr.readexactly(2))[0]
            if cmd != 1:
                cw.write(b'\x05\x07\x00\x01' + bytes(6))
                return cw.close()
            cw.write(b'\x05\x00\x00\x01' + bytes(6))
            await relay(cr, cw, host, port)
            return
        head = b + await cr.readuntil(b'\r\n\r\n')        # HTTP proxy (phones only speak this)
        method, target = head.split(b' ', 2)[:2]
        if target.startswith(b'/'):                       # http://<host>:<port>/proxy.pac
            body = PAC.replace('HOST', '%s:%d' % (cw.get_extra_info('sockname')[0], PORT)).encode()
            cw.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/x-ns-proxy-autoconfig\r\n'
                     b'Content-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            await cw.drain()
            cw.close()
        elif method == b'CONNECT':
            host, _, port = target.decode().rpartition(':')
            cw.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            await relay(cr, cw, host.strip('[]'), int(port))
        else:                                             # plain http://host/... request
            host, _, port = target.split(b'/')[2].decode().partition(':')
            lines = [l for l in head.split(b'\r\n')[:-2] if not l.lower().startswith(
                (b'connection:', b'proxy-connection:', b'keep-alive:'))]
            # browsers reuse a proxy connection for other hosts; force one request per connection
            await relay(cr, cw, host, int(port or 80), b'\r\n'.join(lines + [b'Connection: close', b'', b'']))
    except ERRORS:
        cw.close()


def self_test():
    # fake ClientHello with SNI "www.youtube.com": the split must keep every byte and hide the name
    sni = b'www.youtube.com'
    ext = struct.pack('>HHHBH', 0, len(sni) + 5, len(sni) + 3, 0, len(sni)) + sni
    hs = b'\x03\x03' + bytes(32) + b'\x00' + b'\x00\x02\x13\x01' + b'\x01\x00' + struct.pack('>H', len(ext)) + ext
    hs = b'\x01' + len(hs).to_bytes(3, 'big') + hs
    rec = b'\x16\x03\x01' + struct.pack('>H', len(hs)) + hs
    assert rec[sni_offset(rec):sni_offset(rec) + len(sni)] == sni
    recs = split_hello(rec)
    assert len(recs) == 3 and b''.join(r[5:] for r in recs) == hs
    assert sni not in b''.join(recs)
    assert sni_offset(rec[:20]) is None and len(split_hello(rec[:20])) == 2   # truncated hello must not crash
    assert STRATEGY in ('none', 'split', 'disorder'), 'YT_STRATEGY must be none, split or disorder'


async def self_test_connect():
    srv = await asyncio.start_server(lambda r, w: w.close(), '127.0.0.1', 0)
    port = srv.sockets[0].getsockname()[1]
    (await connect('127.0.0.1', port)).close()
    srv.close()
    await srv.wait_closed()
    t = time.monotonic()
    try:
        (await connect('127.0.0.1', port)).close()
        raise AssertionError('connect to a closed port must fail')
    except ConnectionRefusedError:
        assert time.monotonic() - t < 1, 'a refused port must fail fast, not wait for retries'


async def main():
    self_test()
    await self_test_connect()
    srv = await asyncio.start_server(handle, '0.0.0.0', PORT)
    print('yt-proxy on :%d, strategy %s, PAC at /proxy.pac' % (PORT, STRATEGY), flush=True)
    async with srv:
        await srv.serve_forever()

if __name__ == '__main__':
    asyncio.run(main())
