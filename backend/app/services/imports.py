from __future__ import annotations

import ipaddress
import socket
import zipfile
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.config import Settings
from app.core.errors import AppError


TEXT_EXTENSIONS = {".txt", ".md", ".markdown"}
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, _attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        elif not self._skip and tag in {"p", "br", "div", "article", "section", "h1", "h2", "h3", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        elif not self._skip and tag in {"p", "div", "article", "section", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def parse_uploaded_document(filename: str, data: bytes, max_bytes: int) -> tuple[str, str]:
    if not data:
        raise AppError(422, "empty_document", "上传文档为空")
    if len(data) > max_bytes:
        raise AppError(413, "document_too_large", "上传文档超过大小限制")
    extension = Path(filename).suffix.lower()
    if extension in TEXT_EXTENSIONS:
        if b"\x00" in data[:4096]:
            raise AppError(422, "invalid_text_document", "文本文件包含二进制内容")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AppError(422, "invalid_text_encoding", "文本文件必须使用 UTF-8 编码") from exc
        if not text.strip():
            raise AppError(422, "empty_document", "文档没有可见文本")
        return text.strip(), "text/markdown" if extension in {".md", ".markdown"} else "text/plain"
    if extension == ".docx":
        if not data.startswith(b"PK"):
            raise AppError(422, "invalid_docx", "DOCX 文件签名无效")
        text = _extract_docx(data, max_bytes)
        return text, DOCX_MIME
    raise AppError(415, "unsupported_document_type", "仅支持 .txt、.md、.docx")


def _extract_docx(data: bytes, max_bytes: int) -> str:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            expanded_size = sum(item.file_size for item in archive.infolist())
            if expanded_size > max_bytes * 20:
                raise AppError(413, "docx_expanded_too_large", "DOCX 解压后内容过大")
            if "word/document.xml" not in archive.namelist():
                raise AppError(422, "invalid_docx", "DOCX 缺少正文")
        document = Document(BytesIO(data))
    except AppError:
        raise
    except Exception as exc:
        raise AppError(422, "invalid_docx", "DOCX 文件损坏或无法解析") from exc

    lines: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = "".join(node.text or "" for node in block._element.iter(qn("w:t"))).strip()
            if text:
                lines.append(text)
        elif isinstance(block, Table):
            for row in block.rows:
                values = []
                for cell in row.cells:
                    value = "".join(node.text or "" for node in cell._element.iter(qn("w:t"))).strip()
                    values.append(value)
                if any(values):
                    lines.append("\t".join(values))
    text = "\n".join(lines).strip()
    if not text:
        raise AppError(422, "empty_document", "DOCX 没有可见文本")
    return text


def _is_forbidden_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_url_target(url: str, settings: Settings, resolver=socket.getaddrinfo) -> str:
    if not settings.allow_external_doc_import:
        raise AppError(403, "url_import_disabled", "在线文档导入未启用")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AppError(422, "invalid_url", "仅允许带主机名的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password:
        raise AppError(422, "url_credentials_forbidden", "URL 不得包含用户名或密码")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise AppError(422, "unsafe_url", "拒绝本机或局域网地址")
    try:
        if _is_forbidden_ip(hostname):
            raise AppError(422, "unsafe_url", "拒绝本机、私有或保留地址")
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    if not settings.doc_import_allowed_hosts:
        raise AppError(403, "url_allowlist_required", "启用在线文档导入时必须配置非空域名允许列表")
    if hostname not in settings.doc_import_allowed_hosts:
        raise AppError(403, "host_not_allowed", "目标域名不在允许列表中", {"host": hostname})
    if is_ip_literal:
        return hostname
    try:
        records = resolver(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise AppError(422, "dns_resolution_failed", "无法解析目标域名") from exc
    addresses = {record[4][0] for record in records}
    if not addresses or any(_is_forbidden_ip(address) for address in addresses):
        raise AppError(422, "unsafe_url", "DNS 解析结果包含本机、私有或保留地址")
    return hostname


async def fetch_external_document(url: str, settings: Settings) -> tuple[str, str]:
    current_url = url
    request_timeout = settings.comfyui_client_timeout_seconds or 5.0
    timeout = httpx.Timeout(request_timeout, connect=request_timeout)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        for redirect_count in range(4):
            pinned_url, hostname, host_header = pin_public_url(current_url, settings)
            try:
                async with client.stream("GET", pinned_url, headers={"Accept": "text/html,text/plain,text/markdown", "Host": host_header}, extensions={"sni_hostname": hostname}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect_count >= 3:
                            raise AppError(422, "too_many_redirects", "在线文档重定向次数过多")
                        location = response.headers.get("location")
                        if not location:
                            raise AppError(422, "invalid_redirect", "重定向缺少目标地址")
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status_code >= 400:
                        raise AppError(422, "remote_fetch_failed", f"在线文档返回 HTTP {response.status_code}")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in {"text/html", "text/plain", "text/markdown", "application/xhtml+xml"}:
                        raise AppError(415, "unsupported_remote_type", "在线文档类型不受支持")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > settings.import_max_bytes:
                            raise AppError(413, "document_too_large", "在线文档超过大小限制")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    encoding = response.encoding or "utf-8"
                    try:
                        decoded = raw.decode(encoding)
                    except (LookupError, UnicodeDecodeError):
                        decoded = raw.decode("utf-8", errors="replace")
                    if content_type in {"text/html", "application/xhtml+xml"}:
                        parser = _TextExtractor()
                        parser.feed(decoded)
                        decoded = parser.text()
                    decoded = decoded.strip()
                    if not decoded:
                        raise AppError(422, "empty_document", "在线文档没有可见文本")
                    return decoded, current_url
            except httpx.TimeoutException as exc:
                raise AppError(504, "remote_timeout", "在线文档请求超时") from exc
            except httpx.RequestError as exc:
                raise AppError(422, "remote_fetch_failed", "在线文档请求失败") from exc
    raise AppError(422, "remote_fetch_failed", "在线文档请求失败")


def pin_public_url(url: str, settings: Settings, resolver=socket.getaddrinfo) -> tuple[str, str, str]:
    """Connect to a validated IP literal, retaining original Host and TLS SNI."""
    hostname = validate_url_target(url, settings, resolver)
    parsed = urlsplit(url)
    try:
        records = resolver(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise AppError(422, "dns_resolution_failed", "无法解析目标域名") from exc
    addresses = {record[4][0] for record in records}
    if not addresses or any(_is_forbidden_ip(address) for address in addresses):
        raise AppError(422, "unsafe_url", "连接前 DNS 解析包含禁止地址")
    address = sorted(addresses)[0]
    netloc = f"[{address}]" if ":" in address else address
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, "")), hostname, parsed.netloc


def url_without_query_or_fragment(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

