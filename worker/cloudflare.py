"""Cloudflare tunnel management, worker-side."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import Optional

log = logging.getLogger("worker.cloudflare")

_QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_RATE_LIMIT_RE = re.compile(r"(429|1015|rate.?limit)", re.IGNORECASE)


def ensure_cloudflared(bin_path: str = "/tmp/cloudflared") -> str:
    """Download cloudflared if absent; returns path."""
    if os.path.exists(bin_path):
        return bin_path
    log.info("downloading cloudflared -> %s", bin_path)
    subprocess.run(
        ["wget", "-q",
         "https://github.com/cloudflare/cloudflared/releases/latest/download/"
         "cloudflared-linux-amd64", "-O", bin_path], check=True)
    os.chmod(bin_path, 0o755)
    return bin_path


def parse_quick_url(log_path: str) -> Optional[str]:
    """Extract the most recent .trycloudflare.com URL from a quick-tunnel log."""
    try:
        with open(log_path, "r", errors="ignore") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    urls = _QUICK_URL_RE.findall(text)
    return urls[-1] if urls else None


def _log_has_rate_limit(log_path: str) -> bool:
    """Detect a Cloudflare rate-limit response in the tunnel log."""
    try:
        with open(log_path, "r", errors="ignore") as f:
            tail = f.read()[-2000:]
    except FileNotFoundError:
        return False
    return bool(_RATE_LIMIT_RE.search(tail))


class QuickTunnel:
    """Run/restart a cloudflared quick tunnel and track its public URL."""

    def __init__(self, local_port: int, log_path: str,
                 bin_path: str = "/tmp/cloudflared") -> None:
        self.local_port = local_port
        self.log_path = log_path
        self.bin = ensure_cloudflared(bin_path)
        self.proc: Optional[subprocess.Popen] = None
        self.public_url: Optional[str] = None
        self._log_fd = None

    def start(self) -> None:
        self.stop()
        os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
        self._log_fd = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [self.bin, "tunnel",
             "--url", f"http://127.0.0.1:{self.local_port}",
             "--no-autoupdate",
             "--protocol", "http2",
             "--edge-ip-version", "auto"],
            stdout=self._log_fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def is_alive(self) -> bool:
        if self.proc is None:
            return bool(self.public_url)
        if self.proc.poll() is not None:
            return False
        if not self.public_url:
            self.public_url = parse_quick_url(self.log_path)
        return bool(self.public_url)

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        if self._log_fd is not None:
            try:
                self._log_fd.close()
            except Exception:
                pass
            self._log_fd = None

    def wait_for_url(self, timeout_s: int = 60) -> Optional[str]:
        deadline = time.time() + timeout_s
        consecutive_deaths = 0
        while time.time() < deadline:
            url = parse_quick_url(self.log_path)
            if url:
                self.public_url = url
                return url

            if self.proc and self.proc.poll() is not None:
                consecutive_deaths += 1
                if _log_has_rate_limit(self.log_path):
                    log.error("Cloudflare rate limit (429/1015) - attente 120s")
                    time.sleep(120)
                    self.start()
                    continue
                if consecutive_deaths >= 3:
                    log.error("cloudflared est mort 3 fois - arret definitif")
                    return None
                backoff = min(5 * (2 ** (consecutive_deaths - 1)), 30)
                log.warning("cloudflared died (%d/3) - attente %ds",
                            consecutive_deaths, backoff)
                time.sleep(backoff)
                self.start()
            else:
                time.sleep(1)

        return None


def named_tunnel_url(sub_id: str, domain: str) -> str:
    """Deterministic public URL for a named tunnel with one known hostname."""
    return f"https://{sub_id}.{domain}"
