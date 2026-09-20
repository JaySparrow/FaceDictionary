"""M1: in-process network kill switch."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

import pytest

from facedict import netguard


@pytest.fixture
def guard():
    netguard.install()
    yield
    netguard.uninstall()


def test_blocks_dns_and_tcp(guard):
    with pytest.raises(netguard.NetworkBlockedError):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(netguard.NetworkBlockedError):
        socket.create_connection(("1.1.1.1", 443), timeout=1)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(netguard.NetworkBlockedError):
            s.connect(("127.0.0.1", 9))
        with pytest.raises(netguard.NetworkBlockedError):
            s.bind(("127.0.0.1", 0))
    finally:
        s.close()


def test_blocks_udp_and_ipv6(guard):
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(netguard.NetworkBlockedError):
            u.sendto(b"x", ("1.1.1.1", 53))
    finally:
        u.close()
    s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        with pytest.raises(netguard.NetworkBlockedError):
            s6.connect(("::1", 9))
    finally:
        s6.close()


def test_blocks_urllib(guard):
    with pytest.raises((urllib.error.URLError, netguard.NetworkBlockedError)):
        urllib.request.urlopen("https://example.com", timeout=2)


def test_local_unix_sockets_still_work(guard):
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        a.sendall(b"ping")
        assert b.recv(4) == b"ping"
    finally:
        a.close()
        b.close()


def test_install_is_idempotent_and_reversible():
    original = socket.getaddrinfo
    netguard.install()
    netguard.install()
    assert netguard.installed()
    netguard.uninstall()
    assert socket.getaddrinfo is original
    assert not netguard.installed()


def test_status_and_seal_report_the_layers(guard):
    from facedict import netguard as ng
    assert "in-process guard" in ng.status()
    assert ng.installed()
