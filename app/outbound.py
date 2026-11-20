"""Constrain HTTP destinations and connect to an address checked by this process."""
import ipaddress
import socket
import urllib.error
import urllib.request
from http.client import HTTPConnection, HTTPSConnection

from .common import http_origin


def _connect_public(address, timeout, source_address, *, allow_loopback):
    host, port = address
    entries = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    usable = []
    for family, kind, protocol, _, endpoint in entries:
        ip = ipaddress.ip_address(endpoint[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ((ip.is_global and not ip.is_multicast and not ip.is_reserved)
                or (allow_loopback and ip.is_loopback)):
            raise OSError("目标地址不在允许范围内")
        usable.append((family, kind, protocol, endpoint))

    error = OSError("没有可用的目标地址")
    for family, kind, protocol, endpoint in usable:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(endpoint)
            return sock
        except OSError as exc:
            error = exc
            sock.close()
    raise error


class DestinationPolicy(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def __init__(self, *, https_only, allowed_hosts, loopback_test):
        super().__init__()
        self.https_only = https_only
        self.allowed_hosts = allowed_hosts
        self.loopback_test = loopback_test

    def _open(self, request, connection_type):
        origin = http_origin(request.full_url)
        if origin is None:
            raise urllib.error.URLError("无效的 HTTP 地址")
        scheme, host, _ = origin
        loopback = self.loopback_test and host in {"127.0.0.1", "::1"}
        if (self.https_only and scheme != "https" and not loopback
                or self.allowed_hosts is not None and host not in self.allowed_hosts):
            raise urllib.error.URLError("目标地址不在允许范围内")

        def connection(*args, **kwargs):
            conn = connection_type(*args, **kwargs)
            conn._create_connection = lambda address, timeout, source: _connect_public(
                address, timeout, source, allow_loopback=loopback,
            )
            return conn

        return self.do_open(connection, request)

    def http_open(self, req):
        return self._open(req, HTTPConnection)

    def https_open(self, req):
        return self._open(req, HTTPSConnection)


class RedirectPolicy(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_redirects: bool):
        self.allow_redirects = allow_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.allow_redirects or http_origin(newurl) is None:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_outbound(request, *, timeout, https_only=True, redirects=False, allowed_hosts=None,
                  loopback_test=False):
    if http_origin(request.full_url) is None:
        raise urllib.error.URLError("无效的 HTTP 地址")
    handlers = [
        urllib.request.ProxyHandler({}),
        DestinationPolicy(https_only=https_only, allowed_hosts=allowed_hosts, loopback_test=loopback_test),
        RedirectPolicy(redirects),
    ]
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)
