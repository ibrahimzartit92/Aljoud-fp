import socket, time

def tcp_ping(host: str, port: int, timeout: float = 1.5):
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return True, int((time.perf_counter() - t0) * 1000)
    except Exception:
        return False, -1

def grade_latency(ms: int) -> str:
    if ms < 0:
        return "offline"
    if ms <= 80:
        return "excellent"
    if ms <= 180:
        return "very_good"
    return "good"
