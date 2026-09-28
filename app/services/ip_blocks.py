# SPDX-License-Identifier: AGPL-3.0-or-later
"""CIDR parsing and matching for application-level abuse blocks."""
from __future__ import annotations

import ipaddress
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import IPBlock


def client_ip(value: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def normalize_cidr(value: str) -> str:
    try:
        network = ipaddress.ip_network((value or "").strip(), strict=False)
    except ValueError as exc:
        raise ValueError("Enter a valid IPv4, IPv6, or CIDR range") from exc
    if network.prefixlen == 0:
        raise ValueError("A whole-internet block is not allowed")
    return str(network)


def matching_block(db: Session, address: str) -> Optional[IPBlock]:
    ip = client_ip(address)
    if ip is None:
        return None
    for block in db.scalars(select(IPBlock).where(IPBlock.is_active.is_(True))):
        try:
            if ip in ipaddress.ip_network(block.cidr, strict=False):
                return block
        except ValueError:
            continue
    return None
