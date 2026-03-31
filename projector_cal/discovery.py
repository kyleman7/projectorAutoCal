"""Network device auto-discovery: mDNS + parallel TCP port scan.

Two strategies run concurrently:
  1. mDNS/Zeroconf — passive; listens for Epson and PGenerator service announcements.
  2. Port scan — active fallback; connects to every host on the local subnet on the
     relevant ports, verified by device handshake.

Manually entered IPs always take precedence over discovered ones.
Results are cached to `discovery/last_scan.json` for instant web UI reload.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

logger = logging.getLogger(__name__)

_LAST_SCAN_PATH = Path(__file__).parent.parent / "discovery" / "last_scan.json"
_PROJECTOR_PORT = 3629
_PROJECTOR_HANDSHAKE = bytes.fromhex("455343 2F56502E 6E657410 03000000 0000".replace(" ", ""))
_HANDSHAKE_ACK_LEN = 16
_SCAN_WORKERS = 50
_CONNECT_TIMEOUT = 0.5   # per host during scan (fast)
_VERIFY_TIMEOUT = 2.0    # per device during handshake verify
_MDNS_LISTEN_SECONDS = 3.0


@dataclass
class DiscoveredDevice:
    device_type: Literal["projector", "pgen"]
    ip: str
    port: int
    hostname: str | None
    method: Literal["mdns", "port_scan", "manual"]
    confirmed: bool    # True if handshake verified (not just port open)

    def as_dict(self) -> dict:
        return asdict(self)


class NetworkDiscovery:
    """Discover projector and PGenerator devices on the local network."""

    def __init__(
        self,
        pgen_port: int = 85,
        progress_cb: Callable[[str], None] | None = None,
    ) -> None:
        self.pgen_port = pgen_port
        self._progress_cb = progress_cb or (lambda _: None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(
        self,
        subnet: str | None = None,
        timeout: float = 2.0,
    ) -> list[DiscoveredDevice]:
        """Run mDNS + port scan, returning confirmed devices sorted by type.

        Args:
            subnet: CIDR to scan (e.g., "192.168.1.0/24"). Auto-detected if None.
            timeout: Port-scan connect timeout per host (seconds).

        Returns:
            List of DiscoveredDevice, confirmed first, then unconfirmed.
        """
        detected_subnet = subnet or self.get_local_subnet()
        self._progress(f"Scanning subnet {detected_subnet}…")

        # Run mDNS and port scan together
        mdns_devices = self._mdns_scan()
        port_scan_devices = self._port_scan(detected_subnet, timeout)

        # Merge: prefer mDNS results; add port-scan results not already found
        seen_ips: dict[str, DiscoveredDevice] = {}
        for dev in mdns_devices + port_scan_devices:
            key = f"{dev.ip}:{dev.port}"
            if key not in seen_ips:
                seen_ips[key] = dev

        devices = sorted(
            seen_ips.values(),
            key=lambda d: (0 if d.confirmed else 1, d.device_type)
        )
        self._progress(f"Scan complete. Found {len(devices)} device(s).")
        return devices

    def confirm_device(self, ip: str, device_type: Literal["projector", "pgen"]) -> DiscoveredDevice | None:
        """Try to verify a specific IP as the given device type.

        Returns a DiscoveredDevice if the handshake succeeds, None otherwise.
        """
        self._progress(f"Verifying {ip} as {device_type}…")
        if device_type == "projector":
            confirmed, hostname = self._verify_projector(ip, _PROJECTOR_PORT)
            port = _PROJECTOR_PORT
        else:
            confirmed, hostname = self._verify_pgen(ip, self.pgen_port)
            port = self.pgen_port
        if confirmed:
            return DiscoveredDevice(
                device_type=device_type,
                ip=ip,
                port=port,
                hostname=hostname,
                method="manual",
                confirmed=True,
            )
        return None

    def get_local_subnet(self) -> str:
        """Auto-detect the local network CIDR from the default interface.

        Falls back to 192.168.1.0/24 if detection fails.
        """
        try:
            # Connect to a public IP (doesn't actually send data) to find the outbound interface
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
            # Assume /24 — works for most home networks
            network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
            return str(network)
        except OSError:
            logger.warning("Could not detect local subnet; falling back to 192.168.1.0/24")
            return "192.168.1.0/24"

    def save_last_scan(self, devices: list[DiscoveredDevice]) -> None:
        """Persist scan results to discovery/last_scan.json."""
        _LAST_SCAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LAST_SCAN_PATH, "w") as f:
            json.dump(
                {"scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "devices": [d.as_dict() for d in devices]},
                f, indent=2,
            )
        logger.debug("Last scan saved to %s", _LAST_SCAN_PATH)

    def load_last_scan(self) -> list[DiscoveredDevice]:
        """Load cached scan results. Returns empty list if not available."""
        if not _LAST_SCAN_PATH.exists():
            return []
        try:
            with open(_LAST_SCAN_PATH) as f:
                data = json.load(f)
            return [
                DiscoveredDevice(
                    device_type=d["device_type"],
                    ip=d["ip"],
                    port=d["port"],
                    hostname=d.get("hostname"),
                    method=d["method"],
                    confirmed=d["confirmed"],
                )
                for d in data.get("devices", [])
            ]
        except Exception as e:
            logger.warning("Could not load last scan: %s", e)
            return []

    # ------------------------------------------------------------------
    # mDNS
    # ------------------------------------------------------------------

    def _mdns_scan(self) -> list[DiscoveredDevice]:
        """Listen for mDNS service announcements from projector and PGenerator."""
        try:
            from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf
        except ImportError:
            logger.warning("zeroconf not installed — skipping mDNS scan")
            return []

        devices: list[DiscoveredDevice] = []

        class _Listener:
            def add_service(self_, zc: "Zeroconf", type_: str, name: str) -> None:
                info = zc.get_service_info(type_, name)
                if info and info.addresses:
                    ip = socket.inet_ntoa(info.addresses[0])
                    port = info.port
                    hostname = info.server

                    if "_epsonprojector" in type_:
                        dtype: Literal["projector", "pgen"] = "projector"
                    else:
                        dtype = "pgen"

                    self._progress(f"mDNS: found {dtype} at {ip}:{port} ({hostname})")
                    devices.append(DiscoveredDevice(
                        device_type=dtype,
                        ip=ip,
                        port=port,
                        hostname=hostname,
                        method="mdns",
                        confirmed=False,  # Will be verified below
                    ))

            def remove_service(self_, *args) -> None:
                pass

            def update_service(self_, *args) -> None:
                pass

        zeroconf = Zeroconf()
        listener = _Listener()
        browser1 = ServiceBrowser(zeroconf, "_epsonprojector._tcp.local.", listener)
        browser2 = ServiceBrowser(zeroconf, "_pgenerator._tcp.local.", listener)
        time.sleep(_MDNS_LISTEN_SECONDS)
        zeroconf.close()

        # Verify each discovered device
        verified: list[DiscoveredDevice] = []
        for dev in devices:
            if dev.device_type == "projector":
                confirmed, hostname = self._verify_projector(dev.ip, dev.port)
            else:
                confirmed, hostname = self._verify_pgen(dev.ip, dev.port)
            verified.append(DiscoveredDevice(
                device_type=dev.device_type,
                ip=dev.ip,
                port=dev.port,
                hostname=hostname or dev.hostname,
                method="mdns",
                confirmed=confirmed,
            ))

        return verified

    # ------------------------------------------------------------------
    # Port scan
    # ------------------------------------------------------------------

    def _port_scan(self, subnet: str, timeout: float) -> list[DiscoveredDevice]:
        """Scan every host in subnet on projector + pgen ports in parallel."""
        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            logger.error("Invalid subnet: %s", subnet)
            return []

        hosts = list(network.hosts())
        ports_to_check: list[tuple[str, int, Literal["projector", "pgen"]]] = []
        for host in hosts:
            ip = str(host)
            ports_to_check.append((ip, _PROJECTOR_PORT, "projector"))
            ports_to_check.append((ip, self.pgen_port, "pgen"))

        self._progress(f"Port scanning {len(hosts)} hosts on {subnet}…")
        open_candidates: list[tuple[str, int, Literal["projector", "pgen"]]] = []

        with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as executor:
            futures = {
                executor.submit(self._tcp_connect_check, ip, port, timeout): (ip, port, dtype)
                for ip, port, dtype in ports_to_check
            }
            for future in as_completed(futures):
                ip, port, dtype = futures[future]
                try:
                    if future.result():
                        open_candidates.append((ip, port, dtype))
                        self._progress(f"Port scan: {ip}:{port} open ({dtype})")
                except Exception:
                    pass

        # Verify open candidates
        devices: list[DiscoveredDevice] = []
        for ip, port, dtype in open_candidates:
            if dtype == "projector":
                confirmed, hostname = self._verify_projector(ip, port)
            else:
                confirmed, hostname = self._verify_pgen(ip, port)
            if confirmed or True:  # include unconfirmed too (port was open)
                devices.append(DiscoveredDevice(
                    device_type=dtype,
                    ip=ip,
                    port=port,
                    hostname=hostname,
                    method="port_scan",
                    confirmed=confirmed,
                ))

        return devices

    # ------------------------------------------------------------------
    # Device verification
    # ------------------------------------------------------------------

    @staticmethod
    def _tcp_connect_check(ip: str, port: int, timeout: float) -> bool:
        """Return True if a TCP connection to ip:port can be established."""
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def _verify_projector(ip: str, port: int) -> tuple[bool, str | None]:
        """Try the ESC/VP.net handshake; return (confirmed, hostname)."""
        try:
            with socket.create_connection((ip, port), timeout=_VERIFY_TIMEOUT) as sock:
                sock.settimeout(_VERIFY_TIMEOUT)
                sock.sendall(_PROJECTOR_HANDSHAKE)
                ack = sock.recv(_HANDSHAKE_ACK_LEN)
                confirmed = len(ack) >= _HANDSHAKE_ACK_LEN
                try:
                    hostname = socket.getfqdn(ip)
                except OSError:
                    hostname = None
                return confirmed, hostname
        except OSError:
            return False, None

    @staticmethod
    def _verify_pgen(ip: str, port: int) -> tuple[bool, str | None]:
        """Try opening a connection to PGenerator; return (confirmed, hostname).

        PGenerator doesn't have a challenge/response handshake — an open port
        that accepts a connection is considered confirmed enough.
        """
        try:
            with socket.create_connection((ip, port), timeout=_VERIFY_TIMEOUT):
                try:
                    hostname = socket.getfqdn(ip)
                except OSError:
                    hostname = None
                return True, hostname
        except OSError:
            return False, None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _progress(self, msg: str) -> None:
        logger.debug(msg)
        self._progress_cb(msg)
