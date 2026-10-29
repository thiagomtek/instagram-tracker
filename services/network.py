"""How the worker reaches Instagram: through the VPN proxy or directly.

The VPN runs in its own container and exposes an HTTP proxy. When it is
healthy every Instagram request goes through it; when it is down the worker
leaves through the host network, unless the user turned that fallback off in
the interface, in which case web checks wait. The state is published in
``NetworkStatus`` so the interface can show it.
"""

import time
from datetime import timedelta

import requests
from flask import current_app

from database import db
from models.system import NetworkStatus
from services.clock import utc_now

IP_LOOKUP_URL = "https://ipinfo.io/json"
VPN_CHECK_INTERVAL_SECONDS = 120
DIRECT_CHECK_INTERVAL_SECONDS = 600
LOOKUP_TIMEOUT_SECONDS = 10

_last_vpn_check = {"at": None}
_last_direct_check = {"at": None}


def proxy_url():
    return (current_app.config.get("PROFILE_VALIDATION_PROXY") or "").strip() or None


def get_status():
    row = db.session.get(NetworkStatus, 1)
    if row is None:
        row = NetworkStatus(id=1)
        db.session.add(row)
        db.session.flush()
    return row


def _lookup(proxy=None):
    """Return ``(ip, location)`` seen by the outside world; raises if no service answers.

    Public IP services rate-limit shared VPN addresses, so a refusal from one
    of them proves nothing about the tunnel: the next service is tried. Only
    the first one also describes the location.
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None
    last_error = None
    for url, parser in IP_LOOKUP_SERVICES:
        try:
            response = requests.get(url, proxies=proxies, timeout=LOOKUP_TIMEOUT_SECONDS)
            response.raise_for_status()
            return parser(response)
        except (requests.RequestException, KeyError, ValueError) as exc:
            last_error = exc
    raise last_error


def _parse_ipinfo(response):
    data = response.json()
    place = ", ".join(part for part in (data.get("city"), data.get("region"), data.get("country")) if part)
    org = data.get("org")
    return data["ip"], " · ".join(part for part in (place, org) if part) or None


def _parse_plain_ip(response):
    ip = response.text.strip()
    if not ip or len(ip) > 64 or any(char.isspace() for char in ip):
        raise ValueError("resposta não é um endereço IP")
    return ip, None


IP_LOOKUP_SERVICES = (
    (IP_LOOKUP_URL, _parse_ipinfo),
    ("https://api.ipify.org", _parse_plain_ip),
    ("https://ifconfig.me/ip", _parse_plain_ip),
)


def refresh_network_status(force=False):
    """Probe the VPN (and, rarely, the direct address) and publish the result.

    Throttled in-process, so it is cheap to call on every worker loop.
    """
    now_monotonic = time.monotonic()
    if (
        not force
        and _last_vpn_check["at"] is not None
        and now_monotonic - _last_vpn_check["at"] < VPN_CHECK_INTERVAL_SECONDS
    ):
        return get_status()
    _last_vpn_check["at"] = now_monotonic

    status = get_status()
    proxy = proxy_url()
    status.vpn_configured = bool(proxy)
    status.checked_at = utc_now()

    if (
        force
        or _last_direct_check["at"] is None
        or now_monotonic - _last_direct_check["at"] >= DIRECT_CHECK_INTERVAL_SECONDS
        or not status.direct_ip
    ):
        _last_direct_check["at"] = now_monotonic
        try:
            status.direct_ip = _lookup()[0]
        except (requests.RequestException, KeyError, ValueError) as exc:
            current_app.logger.info("Não foi possível descobrir o IP direto: %s", exc)

    if proxy:
        try:
            ip, location = _lookup(proxy)
            if location is None and ip == status.vpn_ip:
                location = status.vpn_location
        except (requests.RequestException, KeyError, ValueError) as exc:
            status.vpn_up = False
            status.vpn_ip = None
            status.vpn_location = None
            detail = f"A VPN não respondeu ({type(exc).__name__})."
        else:
            # Answering with the host's own address would mean the tunnel is not carrying traffic.
            status.vpn_up = not status.direct_ip or ip != status.direct_ip
            status.vpn_ip = ip
            status.vpn_location = location
            detail = None if status.vpn_up else "O proxy respondeu com o mesmo IP da rede direta; túnel inativo."
    else:
        status.vpn_up = False
        status.vpn_ip = None
        status.vpn_location = None
        detail = "Nenhuma VPN configurada."

    route = _decide_route(status)
    if route != status.route:
        status.route_changed_at = utc_now()
    status.route = route
    status.detail = detail
    db.session.commit()
    return status


def vpn_is_avoided(status):
    return bool(status.vpn_avoid_until and status.vpn_avoid_until > utc_now())


def _decide_route(status):
    if not status.vpn_configured:
        return "direct"
    if status.vpn_up:
        # A blocked VPN address is rested in favour of the direct network, but
        # only when the user allows leaving through it.
        if vpn_is_avoided(status) and status.allow_direct_fallback:
            return "direct"
        return "vpn"
    return "direct" if status.allow_direct_fallback else "paused"


def current_route():
    """``(route, proxy)`` for the next web check, from the last published state."""
    status = get_status()
    route = _decide_route(status)
    return route, (proxy_url() if route == "vpn" else None)


def web_checks_blocked_by_route():
    """True when the VPN is down and the user does not allow leaving directly."""
    return current_route()[0] == "paused"


def can_leave_vpn_when_blocked():
    status = get_status()
    return bool(status.vpn_configured and status.allow_direct_fallback)


def avoid_vpn(seconds):
    """Instagram blocked the VPN address: use the direct network for ``seconds``."""
    status = get_status()
    status.vpn_avoid_until = utc_now() + timedelta(seconds=seconds)
    status.route = _decide_route(status)
    status.route_changed_at = utc_now()
    db.session.commit()


def return_to_vpn():
    status = get_status()
    if status.vpn_avoid_until:
        status.vpn_avoid_until = None
        status.route = _decide_route(status)
        status.route_changed_at = utc_now()
        db.session.commit()


def save_block_state(blocked_until, consecutive_blocks):
    status = get_status()
    status.blocked_until = blocked_until
    status.consecutive_blocks = consecutive_blocks
    db.session.commit()


def set_direct_fallback(allowed):
    status = get_status()
    status.allow_direct_fallback = bool(allowed)
    status.route = _decide_route(status)
    status.route_changed_at = utc_now()
    db.session.commit()
    return status
