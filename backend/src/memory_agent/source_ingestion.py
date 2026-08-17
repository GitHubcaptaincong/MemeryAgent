from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit


SUPPORTED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
SKIP_TAGS = {
    "button",
    "canvas",
    "form",
    "iframe",
    "noscript",
    "option",
    "script",
    "select",
    "style",
    "svg",
    "template",
}


class SourceFetchError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class FetchedSource:
    requested_url: str
    final_url: str
    title: str | None
    content: str
    content_type: str
    retrieved_at: datetime
    response_hash: str


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, connect_ip: str, timeout: float):
        super().__init__(host=host, port=port, timeout=timeout)
        self._connect_ip = connect_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, connect_ip: str, timeout: float):
        super().__init__(
            host=host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._connect_ip = connect_ip

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        self._parts.append(data)

    @property
    def title(self) -> str | None:
        title = _normalize_inline(" ".join(self._title_parts))
        return title or None

    @property
    def text(self) -> str:
        return _normalize_text("".join(self._parts))


def _normalize_inline(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_text(value: str) -> str:
    lines = []
    for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        normalized = _normalize_inline(line)
        if normalized and (not lines or lines[-1] != normalized):
            lines.append(normalized)
    return "\n".join(lines).strip()


def extract_readable_content(body: str, content_type: str) -> tuple[str | None, str]:
    if content_type == "text/plain":
        return None, _normalize_text(body)
    parser = _ReadableHTMLParser()
    parser.feed(body)
    parser.close()
    return parser.title, parser.text


def _validate_url(url: str) -> tuple[str, str, int, str]:
    normalized = url.strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise SourceFetchError("invalid_url", "链接必须是完整的 HTTP 或 HTTPS 地址。")
    if parsed.username is not None or parsed.password is not None:
        raise SourceFetchError("url_credentials_not_allowed", "链接不能包含账号或密码。")
    if not parsed.hostname:
        raise SourceFetchError("invalid_url", "链接缺少有效域名。")
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (UnicodeError, ValueError) as exc:
        raise SourceFetchError("invalid_url", "链接域名或端口无效。") from exc
    if not host:
        raise SourceFetchError("invalid_url", "链接缺少有效域名。")
    path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    canonical = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))
    return canonical, host, port, path


def resolve_public_ips(host: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SourceFetchError("dns_resolution_failed", "无法解析这个公开链接的域名。", retryable=True) from exc
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise SourceFetchError("dns_resolution_failed", "无法解析这个公开链接的域名。", retryable=True)
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise SourceFetchError("private_target_blocked", "链接解析到了无效网络地址。") from exc
        if not address.is_global:
            raise SourceFetchError("private_target_blocked", "仅允许访问公网地址，内网与本机地址已被拒绝。")
    return addresses


def _request_once(
    url: str,
    connect_ip: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> HttpResponse:
    _canonical, host, port, path = _validate_url(url)
    parsed = urlsplit(url)
    connection_class = _PinnedHTTPSConnection if parsed.scheme.lower() == "https" else _PinnedHTTPConnection
    connection = connection_class(host, port, connect_ip, timeout_seconds)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "User-Agent": "MemoryAgentSourceFetcher/1.0",
            },
        )
        response = connection.getresponse()
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise SourceFetchError("response_too_large", "网页响应体超过允许大小。")
        headers = {name.lower(): value for name, value in response.getheaders()}
        if headers.get("content-encoding", "").lower() not in {"", "identity"}:
            raise SourceFetchError("unsupported_content_encoding", "网页未按未压缩格式返回内容。")
        return HttpResponse(status_code=response.status, headers=headers, body=body)
    except (TimeoutError, socket.timeout) as exc:
        raise SourceFetchError("fetch_timeout", "读取公开链接超时，请稍后重试。", retryable=True) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise SourceFetchError("fetch_failed", "无法读取这个公开链接，请稍后重试。", retryable=True) from exc
    finally:
        connection.close()


def _decode_body(body: bytes, content_type_header: str) -> str:
    charset_match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type_header, re.I)
    if not charset_match:
        charset_match = re.search(br"<meta[^>]+charset\s*=\s*[\"']?([^\s\"'/>]+)", body[:8192], re.I)
        charset = charset_match.group(1).decode("ascii", errors="ignore") if charset_match else "utf-8"
    else:
        charset = charset_match.group(1)
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def fetch_public_source(
    url: str,
    *,
    max_chars: int,
    max_bytes: int = 2_000_000,
    timeout_seconds: float = 12.0,
    max_redirects: int = 5,
    resolver: Callable[[str, int], list[str]] = resolve_public_ips,
    requester: Callable[..., HttpResponse] = _request_once,
) -> FetchedSource:
    requested_url, _host, _port, _path = _validate_url(url)
    current_url = requested_url
    visited: set[str] = set()

    for redirect_count in range(max_redirects + 1):
        canonical, host, port, _path = _validate_url(current_url)
        if canonical in visited:
            raise SourceFetchError("redirect_loop", "公开链接发生循环跳转。")
        visited.add(canonical)
        connect_ip = resolver(host, port)[0]
        response = requester(
            canonical,
            connect_ip,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
        if response.status_code in REDIRECT_STATUSES:
            location = response.headers.get("location")
            if not location:
                raise SourceFetchError("invalid_redirect", "网页返回了缺少目标地址的跳转。")
            if redirect_count >= max_redirects:
                raise SourceFetchError("too_many_redirects", "公开链接跳转次数过多。")
            current_url = urljoin(canonical, location)
            continue
        if response.status_code < 200 or response.status_code >= 300:
            raise SourceFetchError(
                "upstream_http_error",
                f"公开链接返回 HTTP {response.status_code}，暂时无法解析。",
                retryable=response.status_code >= 500,
            )

        content_type_header = response.headers.get("content-type", "").lower()
        content_type = content_type_header.split(";", 1)[0].strip()
        if content_type not in SUPPORTED_CONTENT_TYPES:
            raise SourceFetchError("unsupported_content_type", "当前只支持 HTML 网页和纯文本链接。")
        decoded = _decode_body(response.body, content_type_header)
        title, content = extract_readable_content(decoded, content_type)
        if not content:
            raise SourceFetchError("empty_extracted_content", "网页中没有解析出可学习的正文。")
        if len(content) > max_chars:
            raise SourceFetchError(
                "extracted_content_too_long",
                f"网页正文超过 {max_chars:,} 字，请改为复制需要学习的部分。",
            )
        return FetchedSource(
            requested_url=requested_url,
            final_url=canonical,
            title=title,
            content=content,
            content_type=content_type,
            retrieved_at=datetime.now(UTC),
            response_hash=hashlib.sha256(response.body).hexdigest(),
        )

    raise SourceFetchError("too_many_redirects", "公开链接跳转次数过多。")
