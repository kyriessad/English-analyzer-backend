"""Measure how long requests/urllib3 takes to unblock a mid-stream read after
response.close() is called from another thread (the bridge cancel path).

A local HTTP server streams an endless body; a worker thread reads it via
requests.iter_lines(); main thread calls response.close() and times the unblock.
"""
import socket
import threading
import time

import requests

PORT = 18881


def serve() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(1)
    conn, _ = srv.accept()
    conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
    )
    try:
        i = 0
        while True:
            chunk = b'{"n": %d}\n' % i
            conn.sendall(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            time.sleep(0.01)
            i += 1
    except OSError:
        pass
    finally:
        conn.close()


def main() -> None:
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    time.sleep(0.2)

    resp = requests.post(
        f"http://127.0.0.1:{PORT}/stream",
        stream=True,
        timeout=30,
    )
    assert resp.status_code == 200, resp.status_code

    got = []
    done = threading.Event()

    def reader() -> None:
        for line in resp.iter_lines(decode_unicode=True):
            got.append(line)
            if len(got) >= 3:
                break
        done.set()

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    while len(got) < 3:
        time.sleep(0.01)
    t0 = time.perf_counter()
    resp.close()
    unblocked = done.wait(timeout=5)
    print(
        f"close() -> reader finished in {time.perf_counter()-t0:.3f}s "
        f"(unblocked={unblocked}) reader_alive={rt.is_alive()}",
    )


if __name__ == "__main__":
    main()
