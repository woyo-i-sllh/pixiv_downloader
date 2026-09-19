#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixiv 批量图片下载器。

默认从 Pixiv 网页接口读取作品信息和原图地址。若 Pixiv 主站不可达，
对单个作品可使用公开图片代理 pixiv.re 作为回退；作者全部作品仍需要能
访问 Pixiv 元数据接口（通常需要代理）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:
    import requests
except ImportError:
    print("缺少 requests。请先双击“修复环境.bat”。", file=sys.stderr)
    raise SystemExit(2)


ROOT = Path(__file__).resolve().parent
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip"}
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
}
DEFAULT_CONFIG: dict[str, Any] = {
    "download_dir": "..",
    "metadata_dir": "metadata",
    "proxy": "",
    "cookie": "",
    "cookies_file": "",
    "user_agent": DEFAULT_USER_AGENT,
    "pixiv_base": "https://www.pixiv.net",
    "image_proxy": "https://pixiv.re",
    "image_source": "auto",
    "jobs": 3,
    "timeout": 20,
    "retries": 2,
    "max_proxy_pages": 200,
    "all_user_works": True,
    "prefer_original": True,
}
_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_PRINT_LOCK = threading.Lock()


class PixivError(RuntimeError):
    """Pixiv 请求或下载失败。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Artwork:
    id: str
    title: str = ""
    user_id: str = ""
    user_name: str = ""
    page_urls: list[str] = field(default_factory=list)
    page_exts: list[str] = field(default_factory=list)
    illust_type: int = 0
    source: str = "pixiv"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DownloadResult:
    path: Path | None
    status: str
    bytes_written: int = 0


def log(message: str, quiet: bool = False) -> None:
    if quiet:
        return
    with _PRINT_LOCK:
        print(message, flush=True)


def log_warning(message: str) -> None:
    with _PRINT_LOCK:
        print(f"[提示] {message}", file=sys.stderr, flush=True)


def load_config(path: Path) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if not path.exists():
        return config
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise PixivError(f"无法读取配置文件 {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PixivError(f"配置文件必须是 JSON 对象: {path}")
    config.update(raw)
    return config


def resolve_local_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def safe_name(value: Any, fallback: str, limit: int = 90) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = _INVALID_WINDOWS_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" .")
    if not text:
        text = fallback
    if text.upper() in _RESERVED_WINDOWS_NAMES:
        text = f"_{text}"
    if len(text) > limit:
        suffix = text[-12:].strip()
        text = text[: max(1, limit - len(suffix) - 1)].rstrip() + "…" + suffix
    return text


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temp, path)


def read_urls_from_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    urls: list[str] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split("#", 1)[0].strip()
        urls.extend(part for part in line.split() if part)
    return urls


def collect_links(args: argparse.Namespace) -> list[str]:
    links: list[str] = list(args.links)
    for file_value in args.input_file or []:
        path = resolve_local_path(file_value)
        links.extend(read_urls_from_file(path))

    if links and not args.interactive:
        return links

    default_urls = ROOT / "urls.txt"
    if not links and default_urls.exists():
        links.extend(read_urls_from_file(default_urls))
    if links and not args.interactive:
        return links

    print("请输入 Pixiv 链接，一行一个；可以一次粘贴多个链接。")
    print("输入空行开始下载，输入 q 退出。")
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if not line:
            break
        if line.lower() in {"q", "quit", "exit"}:
            return []
        links.extend(part for part in line.split() if part)
    return links


def classify_link(link: str) -> tuple[str, str] | None:
    value = link.strip().strip('"').strip("'")
    if not value:
        return None
    lower = value.lower()

    if re.fullmatch(r"\d+", value):
        return "artwork", value

    artwork_patterns = (
        r"(?:artworks?/)(\d+)",
        r"(?:illust_id=|illust_id/)(\d+)",
        r"(?:member_illust\.php[^\s#]*[?&]illust_id=)(\d+)",
        r"(?:illust/)(\d+)",
    )
    for pattern in artwork_patterns:
        match = re.search(pattern, lower)
        if match:
            return "artwork", match.group(1)

    user_patterns = (
        r"(?:users/)(\d+)",
        r"(?:member\.php[^\s#]*[?&]id=)(\d+)",
        r"(?:user_id=)(\d+)",
    )
    for pattern in user_patterns:
        match = re.search(pattern, lower)
        if match:
            return "user", match.group(1)

    if lower.startswith("art:"):
        value = lower.split(":", 1)[1]
        return ("artwork", value) if value.isdigit() else None
    if lower.startswith("user:"):
        value = lower.split(":", 1)[1]
        return ("user", value) if value.isdigit() else None
    return None


def parse_cookie_header(cookie_header: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            result[name] = value.strip()
    return result


def load_cookies_file(session: requests.Session, path: Path) -> None:
    if not path.exists():
        raise PixivError(f"Cookie 文件不存在: {path}")

    if path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise PixivError(f"无法读取 JSON Cookie 文件: {exc}") from exc
        if isinstance(data, dict) and "cookies" in data:
            data = data["cookies"]
        if not isinstance(data, list):
            raise PixivError("JSON Cookie 文件应为数组，或包含 cookies 数组。")
        for item in data:
            if not isinstance(item, dict) or "name" not in item:
                continue
            session.cookies.set(
                str(item["name"]),
                str(item.get("value", "")),
                domain=str(item.get("domain") or ".pixiv.net"),
                path=str(item.get("path") or "/"),
            )
        return

    jar = MozillaCookieJar()
    try:
        jar.load(str(path), ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        raise PixivError(f"无法读取 Netscape 格式 Cookie: {exc}") from exc
    session.cookies.update(jar)


def extension_from_url(url: str) -> str:
    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    return suffix if suffix in IMAGE_EXTENSIONS else ""


def extension_from_response(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type in CONTENT_TYPE_EXTENSIONS:
        return CONTENT_TYPE_EXTENSIONS[content_type]

    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition, re.I)
    if match:
        suffix = Path(unquote(match.group(1))).suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            return suffix

    if content_type.startswith("text/") or content_type in {"application/json", "text/html"}:
        raise PixivError(f"服务器返回的不是图片: {content_type or '未知类型'}")
    return ".jpg"


def is_image_response(response: requests.Response) -> bool:
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    return content_type.startswith("image/") or content_type in {
        "application/zip",
        "application/x-zip-compressed",
    }


class PixivClient:
    def __init__(
        self,
        *,
        pixiv_base: str,
        image_proxy: str,
        proxy: str,
        cookie: str,
        cookies_file: Path | None,
        user_agent: str,
        timeout: float,
        retries: int,
        prefer_original: bool,
    ) -> None:
        self.pixiv_base = pixiv_base.rstrip("/")
        self.image_proxy = image_proxy.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)
        self.prefer_original = prefer_original
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.api_disabled_reason = ""
        self._local = threading.local()

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
            }
        )
        if proxy:
            self.session.proxies.update(self.proxies or {})
        if cookies_file:
            load_cookies_file(self.session, cookies_file)
        for name, value in parse_cookie_header(cookie).items():
            self.session.cookies.set(name, value, domain=".pixiv.net", path="/")

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(self.session.headers)
        session.cookies.update(self.session.cookies)
        if self.proxies:
            session.proxies.update(self.proxies)
        return session

    def thread_session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._new_session()
            self._local.session = session
        return session

    def _disable_api(self, reason: str) -> None:
        if not self.api_disabled_reason:
            self.api_disabled_reason = reason

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        stream: bool = False,
        allow_redirects: bool = True,
        metadata_request: bool = False,
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    timeout=self.timeout,
                    stream=stream,
                    allow_redirects=allow_redirects,
                    proxies=self.proxies,
                )
            except (requests.Timeout, requests.ConnectionError, requests.exceptions.SSLError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 5))
                    continue
                if metadata_request and self.pixiv_base in url:
                    self._disable_api(f"连接 Pixiv 失败: {exc}")
                raise PixivError(
                    "无法连接 Pixiv。若在中国大陆，通常需要配置 --proxy；"
                    "单个公开作品仍可尝试图片代理回退。"
                ) from exc

            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else min(2 ** attempt, 5)
                response.close()
                time.sleep(delay)
                continue

            if metadata_request and response.status_code in {403, 401} and self.pixiv_base in url:
                self._disable_api(f"Pixiv 元数据接口返回 HTTP {response.status_code}")
            return response

        raise PixivError(f"请求失败: {last_error or url}")

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.api_disabled_reason:
            raise PixivError(f"Pixiv 元数据接口已停用: {self.api_disabled_reason}")
        url = f"{self.pixiv_base}{path}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{self.pixiv_base}/",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        response = self._request(
            "GET",
            url,
            headers=headers,
            params=params,
            metadata_request=True,
        )
        if response.status_code != 200:
            status = response.status_code
            response.close()
            raise PixivError(
                f"Pixiv 接口返回 HTTP {status}: {url}",
                status=status,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            preview = response.text[:160].replace("\n", " ")
            response.close()
            raise PixivError(f"Pixiv 返回的不是 JSON，可能触发了验证页: {preview}") from exc
        response.close()
        if not isinstance(payload, dict):
            raise PixivError(f"Pixiv 返回格式异常: {url}")
        if payload.get("error"):
            raise PixivError(str(payload.get("message") or f"Pixiv 接口错误: {url}"))
        return payload

    def get_artwork(self, artwork_id: str) -> Artwork:
        detail_payload = self._request_json(
            f"/ajax/illust/{artwork_id}",
            {"lang": "zh"},
        )
        detail = detail_payload.get("body")
        if not isinstance(detail, dict):
            raise PixivError(f"作品 {artwork_id} 没有返回详情数据。")

        page_count = int(detail.get("pageCount") or 1)
        page_items: list[dict[str, Any]] = []
        try:
            pages_payload = self._request_json(
                f"/ajax/illust/{artwork_id}/pages",
                {"lang": "zh"},
            )
            raw_pages = pages_payload.get("body")
            if isinstance(raw_pages, list):
                page_items = [item for item in raw_pages if isinstance(item, dict)]
        except PixivError as exc:
            log_warning(f"读取作品 {artwork_id} 的分页列表失败，将尝试使用详情页地址: {exc}")

        if not page_items:
            urls = detail.get("urls")
            page_items = [{"urls": urls if isinstance(urls, dict) else {}}]
            if page_count > 1:
                log_warning(f"作品 {artwork_id} 有 {page_count} 页，但未能取得完整分页列表。")

        page_urls: list[str] = []
        page_exts: list[str] = []
        for item in page_items:
            urls = item.get("urls") if isinstance(item.get("urls"), dict) else {}
            if self.prefer_original:
                url = urls.get("original") or urls.get("regular") or urls.get("small")
            else:
                url = urls.get("regular") or urls.get("small") or urls.get("original")
            if not url:
                continue
            if str(url).startswith("//"):
                url = "https:" + str(url)
            page_urls.append(str(url))
            page_exts.append(extension_from_url(str(url)))

        if not page_urls:
            raise PixivError(f"作品 {artwork_id} 没有可下载的图片地址。")

        return Artwork(
            id=str(detail.get("id") or artwork_id),
            title=str(detail.get("title") or ""),
            user_id=str(detail.get("userId") or ""),
            user_name=str(detail.get("userName") or ""),
            page_urls=page_urls,
            page_exts=page_exts,
            illust_type=int(detail.get("illustType") or 0),
            source="pixiv",
            metadata={
                "id": str(detail.get("id") or artwork_id),
                "title": detail.get("title"),
                "description": detail.get("description"),
                "userId": detail.get("userId"),
                "userName": detail.get("userName"),
                "userAccount": detail.get("userAccount"),
                "createDate": detail.get("createDate"),
                "uploadDate": detail.get("uploadDate"),
                "pageCount": detail.get("pageCount"),
                "width": detail.get("width"),
                "height": detail.get("height"),
                "tags": detail.get("tags"),
                "xRestrict": detail.get("xRestrict"),
                "sl": detail.get("sl"),
                "aiType": detail.get("aiType"),
                "illustType": detail.get("illustType"),
                "urls": detail.get("urls"),
            },
        )

    def get_user_works(self, user_id: str) -> list[str]:
        payload = self._request_json(
            f"/ajax/user/{user_id}/profile/all",
            {"lang": "zh"},
        )
        body = payload.get("body")
        if not isinstance(body, dict):
            raise PixivError(f"作者 {user_id} 没有返回作品列表。")

        ids: set[str] = set()
        for key in ("illusts", "manga"):
            items = body.get(key)
            if isinstance(items, dict):
                ids.update(str(value) for value in items.keys() if str(value).isdigit())
            elif isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("id"):
                        ids.add(str(item["id"]))
        return sorted(ids, key=lambda value: int(value), reverse=True)

    def head_proxy_image(self, url: str) -> requests.Response:
        return self._request(
            "HEAD",
            url,
            headers={"Referer": "https://www.pixiv.net/"},
            allow_redirects=True,
        )

    def discover_proxy_artwork(self, artwork_id: str, max_pages: int) -> Artwork:
        if not self.image_proxy:
            raise PixivError("未配置图片代理。")
        route_ext = ""
        first_url = ""
        first_ext = ""
        last_status: int | None = None

        for candidate_ext in ("jpg", "png", "webp", "gif"):
            candidate = f"{self.image_proxy}/{artwork_id}.{candidate_ext}"
            response = self.head_proxy_image(candidate)
            last_status = response.status_code
            if response.status_code == 200 and is_image_response(response):
                route_ext = candidate_ext
                first_url = candidate
                first_ext = (
                    extension_from_response(response)
                    or extension_from_url(response.headers.get("X-Origin-Url", ""))
                    or f".{candidate_ext}"
                )
                response.close()
                break
            response.close()

        if not first_url:
            raise PixivError(
                f"图片代理中未找到作品 {artwork_id}"
                + (f"（HTTP {last_status}）" if last_status else "")
            )

        page_urls = [first_url]
        page_exts = [first_ext]
        for page in range(1, max_pages + 1):
            url = f"{self.image_proxy}/{artwork_id}-{page}.{route_ext}"
            response = self.head_proxy_image(url)
            status = response.status_code
            if status == 200 and is_image_response(response):
                ext = (
                    extension_from_response(response)
                    or extension_from_url(response.headers.get("X-Origin-Url", ""))
                    or first_ext
                )
                page_urls.append(url)
                page_exts.append(ext)
                response.close()
                continue
            response.close()
            if status in {400, 403, 404, 410}:
                break
            log_warning(f"检查图片代理第 {page + 1} 页时返回 HTTP {status}，停止探测。")
            break

        return Artwork(
            id=str(artwork_id),
            title="",
            page_urls=page_urls,
            page_exts=page_exts,
            source="image-proxy",
            metadata={"id": str(artwork_id), "source": self.image_proxy},
        )

    def proxy_url_for_page(self, artwork_id: str, page: int, ext: str) -> str:
        if not self.image_proxy:
            return ""
        safe_ext = ext if ext in IMAGE_EXTENSIONS else ".jpg"
        if page == 0:
            return f"{self.image_proxy}/{artwork_id}{safe_ext}"
        return f"{self.image_proxy}/{artwork_id}-{page}{safe_ext}"

    def _existing_file(self, directory: Path, stem: str) -> Path | None:
        for candidate in directory.glob(f"{stem}.*"):
            if candidate.suffix.lower() == ".part":
                continue
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def download_file(
        self,
        url: str,
        directory: Path,
        stem: str,
        *,
        fallback_url: str = "",
        overwrite: bool = False,
        quiet: bool = False,
    ) -> DownloadResult:
        if not overwrite:
            existing = self._existing_file(directory, stem)
            if existing:
                return DownloadResult(existing, "skipped", existing.stat().st_size)

        candidates = [url]
        if fallback_url and fallback_url != url:
            candidates.append(fallback_url)
        errors: list[str] = []

        for index, candidate in enumerate(candidates):
            try:
                return self._download_one(
                    candidate,
                    directory,
                    stem,
                    overwrite=overwrite,
                    quiet=quiet,
                )
            except PixivError as exc:
                errors.append(str(exc))
                if index + 1 < len(candidates):
                    log_warning(f"{stem} 直连失败，切换图片代理: {exc}")
        raise PixivError("; ".join(errors) or f"下载失败: {url}")

    def _download_one(
        self,
        url: str,
        directory: Path,
        stem: str,
        *,
        overwrite: bool,
        quiet: bool,
    ) -> DownloadResult:
        directory.mkdir(parents=True, exist_ok=True)
        session = self.thread_session()
        headers = {
            "Referer": "https://www.pixiv.net/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        response = session.get(
            url,
            headers=headers,
            timeout=self.timeout,
            stream=True,
            allow_redirects=True,
            proxies=self.proxies,
        )
        try:
            if response.status_code == 404:
                raise PixivError(f"文件不存在 HTTP 404: {url}", status=404)
            if response.status_code == 403:
                raise PixivError(f"图片被拒绝 HTTP 403: {url}", status=403)
            if response.status_code >= 400:
                raise PixivError(f"下载返回 HTTP {response.status_code}: {url}", status=response.status_code)

            extension = extension_from_url(url) or extension_from_response(response)
            if not extension:
                extension = ".jpg"
            destination = directory / f"{stem}{extension}"
            if destination.exists() and destination.stat().st_size > 0 and not overwrite:
                return DownloadResult(destination, "skipped", destination.stat().st_size)

            part = Path(str(destination) + ".part")
            resume_from = part.stat().st_size if part.exists() and not overwrite else 0
            if resume_from:
                response.close()
                response = session.get(
                    url,
                    headers={**headers, "Range": f"bytes={resume_from}-"},
                    timeout=self.timeout,
                    stream=True,
                    allow_redirects=True,
                    proxies=self.proxies,
                )
                if response.status_code >= 400:
                    part.unlink(missing_ok=True)
                    resume_from = 0
                    response.close()
                    response = session.get(
                        url,
                        headers=headers,
                        timeout=self.timeout,
                        stream=True,
                        allow_redirects=True,
                        proxies=self.proxies,
                    )
                elif response.status_code != 206:
                    resume_from = 0

            mode = "ab" if resume_from and response.status_code == 206 else "wb"
            if mode == "wb":
                resume_from = 0
            content_length = int(response.headers.get("Content-Length") or 0)
            expected = resume_from + content_length if content_length else 0
            written = 0
            started = time.monotonic()
            if not quiet:
                log(f"  下载 {destination.name} ...")

            with part.open(mode) as file_obj:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    file_obj.write(chunk)
                    written += len(chunk)

            actual_size = part.stat().st_size
            if expected and actual_size != expected:
                raise PixivError(
                    f"下载不完整: {destination.name} ({actual_size}/{expected} 字节)，"
                    "可再次运行以断点续传。"
                )
            os.replace(part, destination)
            elapsed = max(time.monotonic() - started, 0.001)
            speed = written / elapsed / 1024 / 1024
            if not quiet:
                log(
                    f"  完成 {destination.name} "
                    f"({written / 1024 / 1024:.2f} MB, {speed:.2f} MB/s)"
                )
            return DownloadResult(destination, "downloaded", actual_size)
        finally:
            response.close()


def build_proxy_page(artwork_id: str, page: int, ext: str, image_proxy: str) -> str:
    if not image_proxy:
        return ""
    safe_ext = ext if ext in IMAGE_EXTENSIONS else ".jpg"
    if page == 0:
        return f"{image_proxy.rstrip('/')}/{artwork_id}{safe_ext}"
    return f"{image_proxy.rstrip('/')}/{artwork_id}-{page}{safe_ext}"


def resolve_settings(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    settings = dict(config)
    if args.output_dir is not None:
        settings["download_dir"] = args.output_dir
    for name in (
        "proxy",
        "cookie",
        "cookies_file",
        "user_agent",
        "pixiv_base",
        "image_proxy",
        "image_source",
        "jobs",
        "timeout",
        "retries",
        "max_proxy_pages",
    ):
        value = getattr(args, name, None)
        if value is not None:
            settings[name] = value
    if args.no_image_proxy:
        settings["image_proxy"] = ""
    if args.all_user_works is not None:
        settings["all_user_works"] = args.all_user_works
    if args.prefer_original is not None:
        settings["prefer_original"] = args.prefer_original

    settings["download_dir"] = str(resolve_local_path(settings["download_dir"]))
    cookies_file = str(settings.get("cookies_file") or "").strip()
    settings["cookies_file_path"] = resolve_local_path(cookies_file) if cookies_file else None
    settings["jobs"] = max(1, int(settings.get("jobs") or 1))
    settings["timeout"] = max(1.0, float(settings.get("timeout") or 20))
    settings["retries"] = max(0, int(settings.get("retries") or 0))
    settings["max_proxy_pages"] = max(1, int(settings.get("max_proxy_pages") or 200))
    settings["image_source"] = str(settings.get("image_source") or "auto").lower()
    if settings["image_source"] not in {"auto", "pixiv", "proxy"}:
        raise PixivError("image_source 只能是 auto、pixiv 或 proxy。")
    return settings


def artwork_folder(root: Path, artwork: Artwork) -> Path:
    artist_name = safe_name(
        f"{artwork.user_name or '未知作者'} ({artwork.user_id or 'unknown'})",
        "未知作者 (unknown)",
        limit=80,
    )
    return root / artist_name


def download_artwork(
    client: PixivClient,
    artwork: Artwork,
    root: Path,
    *,
    settings: dict[str, Any],
    overwrite: bool,
    dry_run: bool,
    metadata_only: bool,
    quiet: bool,
    counter: dict[str, int],
    counter_lock: threading.Lock,
) -> None:
    folder = artwork_folder(root, artwork)
    fallback_proxy = str(settings.get("image_proxy") or "")
    image_source = settings["image_source"]

    metadata_root = root / str(settings.get("metadata_dir") or "metadata")
    metadata_path = metadata_root / folder.name / f"{artwork.id}.json"
    if not dry_run:
        metadata = dict(artwork.metadata)
        metadata.setdefault("id", artwork.id)
        metadata.setdefault("title", artwork.title)
        metadata.setdefault("userId", artwork.user_id)
        metadata.setdefault("userName", artwork.user_name)
        metadata.setdefault("source", artwork.source)
        metadata.setdefault("pageCount", len(artwork.page_urls))
        atomic_write_json(metadata_path, metadata)

    log(
        f"[作品 {artwork.id}] {artwork.title or '未命名'} "
        f"({len(artwork.page_urls)} 页) -> {folder}"
    )
    if metadata_only or dry_run:
        for page, url in enumerate(artwork.page_urls):
            log(f"  p{page}: {url}")
        return

    file_base = safe_name(
        (f"{artwork.title or 'untitled'}-{artwork.user_name}-{artwork.id}" if artwork.user_name else f"{artwork.title or 'untitled'}-{artwork.id}"),
        f"untitled-{artwork.id}",
        limit=120,
    )
    tasks: list[tuple[str, str, str]] = []
    for page, direct_url in enumerate(artwork.page_urls):
        ext = artwork.page_exts[page] if page < len(artwork.page_exts) else ""
        proxy_url = build_proxy_page(artwork.id, page, ext, fallback_proxy)
        if image_source == "proxy":
            primary = proxy_url or direct_url
            fallback = direct_url if proxy_url else ""
        elif image_source == "pixiv":
            primary = direct_url
            fallback = ""
        else:
            primary = direct_url
            fallback = proxy_url
        stem = file_base if len(artwork.page_urls) == 1 else f"{file_base}_p{page}"
        tasks.append((primary, fallback, stem))

    def run_task(task: tuple[str, str, str]) -> DownloadResult:
        primary, fallback, stem = task
        return client.download_file(
            primary,
            folder,
            stem,
            fallback_url=fallback,
            overwrite=overwrite,
            quiet=quiet,
        )

    if settings["jobs"] == 1 or len(tasks) == 1:
        results = [run_task(task) for task in tasks]
    else:
        with ThreadPoolExecutor(max_workers=settings["jobs"]) as pool:
            futures = {pool.submit(run_task, task): task for task in tasks}
            results = []
            for future in as_completed(futures):
                results.append(future.result())

    with counter_lock:
        for result in results:
            counter["images"] += 1
            counter[result.status] = counter.get(result.status, 0) + 1
            if result.status == "downloaded":
                counter["bytes"] += result.bytes_written
            elif result.status == "skipped":
                counter["skipped_bytes"] += result.bytes_written


def print_summary(counter: dict[str, int]) -> None:
    print()
    print("下载完成")
    print(f"  作品: {counter.get('artworks', 0)}")
    print(f"  图片: {counter.get('images', 0)}")
    print(f"  新下载: {counter.get('downloaded', 0)}")
    print(f"  已存在: {counter.get('skipped', 0)}")
    print(f"  失败作品: {counter.get('failed_artworks', 0)}")
    print(f"  新下载大小: {counter.get('bytes', 0) / 1024 / 1024:.2f} MB")
    print(f"  已有文件大小: {counter.get('skipped_bytes', 0) / 1024 / 1024:.2f} MB")


def prompt_links(title: str) -> list[str]:
    print()
    print(title)
    print("每行可粘贴一个或多个链接；直接回车开始，输入 b 返回主菜单。")
    links: list[str] = []
    while True:
        try:
            value = input(f"[{len(links) + 1}] ").strip()
        except EOFError:
            break
        if not value:
            break
        if value.lower() == "b":
            return []
        links.extend(part for part in value.split() if part)
    return links


def menu_download_root() -> Path:
    config = load_config(ROOT / "config.json")
    return resolve_local_path(config.get("download_dir") or "..")


def update_menu_config(updates: dict[str, Any]) -> None:
    config_path = ROOT / "config.json"
    config = load_config(config_path)
    config.update(updates)
    atomic_write_json(config_path, config)


def show_menu() -> int:
    while True:
        try:
            os.system("cls")
            config = load_config(ROOT / "config.json")
            proxy = str(config.get("proxy") or "系统环境/直连")
            cookie_file = str(config.get("cookies_file") or "未设置")
            print("=" * 68)
            print(" Pixiv 下载器")
            print("=" * 68)
            print(f" 下载目录 : {menu_download_root()}")
            print(f" JSON目录 : {menu_download_root() / str(config.get('metadata_dir') or 'metadata')}")
            print(f" 代理     : {proxy}")
            print(f" Cookie   : {cookie_file}")
            print("-" * 68)
            print(" 1. 下载作品链接（支持一次输入多个）")
            print(" 2. 下载作者全部作品")
            print(" 3. 从 urls.txt 批量下载")
            print(" 4. 打开下载目录")
            print(" 5. 修改代理")
            print(" 6. 设置 Cookie 文件")
            print(" 0. 退出")
            print("-" * 68)
            choice = input("请选择: ").strip()

            if choice == "0":
                return 0
            if choice == "1":
                links = prompt_links("请输入作品链接：")
                if links:
                    main(links)
                    input("\n按回车返回主菜单...")
                continue
            if choice == "2":
                links = prompt_links("请输入作者主页链接：")
                if links:
                    limit_text = input("每个作者最多下载几件？直接回车表示全部: ").strip()
                    extra = ["--limit-works", limit_text] if limit_text.isdigit() and int(limit_text) > 0 else []
                    main(links + extra)
                    input("\n按回车返回主菜单...")
                continue
            if choice == "3":
                main(["-i", str(ROOT / "urls.txt")])
                input("\n按回车返回主菜单...")
                continue
            if choice == "4":
                target = menu_download_root()
                target.mkdir(parents=True, exist_ok=True)
                os.startfile(str(target))  # type: ignore[attr-defined]
                continue
            if choice == "5":
                print()
                value = input("新代理地址，例如 http://127.0.0.1:7877；输入 direct 可清除: ").strip()
                if value:
                    update_menu_config({"proxy": "" if value.lower() in {"direct", "off", "none"} else value})
                    print("代理设置已保存。")
                    time.sleep(1)
                continue
            if choice == "6":
                print()
                value = input("Cookie 文件路径；输入 direct 可清除: ").strip()
                if value:
                    update_menu_config({"cookies_file": "" if value.lower() in {"direct", "off", "none"} else value})
                    print("Cookie 设置已保存。")
                    time.sleep(1)
                continue
            print("无效选项，请重新选择。")
            time.sleep(1)
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pixiv 批量图片下载器：支持多个作品链接、作者全部作品、代理和 Cookie。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python pixiv_downloader.py https://www.pixiv.net/artworks/148537766\n"
            "  python pixiv_downloader.py URL1 URL2 --proxy http://127.0.0.1:7890\n"
            "  python pixiv_downloader.py https://www.pixiv.net/users/123456 --limit-works 20\n"
            "  python pixiv_downloader.py -i urls.txt\n"
        ),
    )
    parser.add_argument("links", nargs="*", help="Pixiv 作品链接或作者主页链接，可一次给多个。")
    parser.add_argument("-i", "--input-file", action="append", help="从文本文件读取链接，可重复指定。")
    parser.add_argument("--interactive", action="store_true", help="强制进入交互式链接输入。")
    parser.add_argument("--menu", action="store_true", help="启动菜单式交互界面。")
    parser.add_argument("-o", "--output-dir", help="下载目录，默认是脚本旁的 downloads。")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="配置文件路径。")
    parser.add_argument("--proxy", help="HTTP/HTTPS 代理，例如 http://127.0.0.1:7890。")
    parser.add_argument("--cookie", help="直接传入 Cookie 字符串。")
    parser.add_argument("--cookies-file", help="Cookie 文件路径，支持 Netscape cookies.txt 或 JSON。")
    parser.add_argument("--user-agent", help="自定义 User-Agent。")
    parser.add_argument("--pixiv-base", help="Pixiv 主站地址，默认 https://www.pixiv.net。")
    parser.add_argument(
        "--image-proxy",
        help="图片回退代理，默认 https://pixiv.re；填 off 可禁用。",
    )
    parser.add_argument("--no-image-proxy", action="store_true", help="禁用图片回退代理。")
    parser.add_argument(
        "--image-source",
        choices=("auto", "pixiv", "proxy"),
        help="图片通道：auto 直连失败后回退，pixiv 只用原站，proxy 优先图片代理。",
    )
    parser.add_argument("--jobs", type=int, help="同作品图片并发数，默认 3。")
    parser.add_argument("--timeout", type=float, help="单次网络超时秒数，默认 20。")
    parser.add_argument("--retries", type=int, help="失败重试次数，默认 2。")
    parser.add_argument("--max-proxy-pages", type=int, help="图片代理最多探测页数，默认 200。")
    parser.add_argument("--limit-works", type=int, help="每个作者最多下载多少件作品。")
    parser.add_argument(
        "--all-user-works",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="作者主页链接是否下载全部作品，默认开启。",
    )
    parser.add_argument(
        "--prefer-original",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="优先原图，默认开启；--no-prefer-original 使用 regular。",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在文件。")
    parser.add_argument("--metadata-only", action="store_true", help="只保存 metadata.json，不下载图片。")
    parser.add_argument("--dry-run", action="store_true", help="只显示会下载的内容，不写文件。")
    parser.add_argument("-q", "--quiet", action="store_true", help="减少输出。")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(resolve_local_path(args.config))
        settings = resolve_settings(args, config)
    except PixivError as exc:
        parser.error(str(exc))

    if str(settings.get("image_proxy", "")).lower() in {"off", "none", "false"}:
        settings["image_proxy"] = ""

    if args.menu:
        return show_menu()

    links = collect_links(args)
    if not links:
        print("没有输入链接。", file=sys.stderr)
        return 2

    parsed_items: list[tuple[str, str, str]] = []
    seen_inputs: set[tuple[str, str]] = set()
    for link in links:
        parsed = classify_link(link)
        if not parsed:
            log_warning(f"无法识别链接，已跳过: {link}")
            continue
        key = (parsed[0], parsed[1])
        if key in seen_inputs:
            continue
        seen_inputs.add(key)
        parsed_items.append((parsed[0], parsed[1], link))

    if not parsed_items:
        print("没有可识别的 Pixiv 作品或作者链接。", file=sys.stderr)
        return 2

    client = PixivClient(
        pixiv_base=str(settings["pixiv_base"]),
        image_proxy=str(settings.get("image_proxy") or ""),
        proxy=str(settings.get("proxy") or ""),
        cookie=str(settings.get("cookie") or ""),
        cookies_file=settings.get("cookies_file_path"),
        user_agent=str(settings.get("user_agent") or DEFAULT_USER_AGENT),
        timeout=float(settings["timeout"]),
        retries=int(settings["retries"]),
        prefer_original=bool(settings["prefer_original"]),
    )

    root = Path(settings["download_dir"])
    if not args.dry_run:
        root.mkdir(parents=True, exist_ok=True)
    counter: dict[str, int] = {
        "artworks": 0,
        "images": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed_artworks": 0,
        "bytes": 0,
        "skipped_bytes": 0,
    }
    processed_artworks: set[str] = set()
    limit_works = max(0, int(args.limit_works or 0))

    def process_artwork(artwork_id: str) -> None:
        if artwork_id in processed_artworks:
            return
        processed_artworks.add(artwork_id)
        artwork: Artwork
        try:
            if client.api_disabled_reason and settings["image_source"] != "pixiv":
                raise PixivError(client.api_disabled_reason)
            artwork = client.get_artwork(artwork_id)
        except PixivError as exc:
            if settings["image_source"] == "pixiv" or not settings.get("image_proxy"):
                log_warning(f"作品 {artwork_id} 获取失败: {exc}")
                counter["failed_artworks"] += 1
                return
            log_warning(f"作品 {artwork_id} 元数据接口不可用，改用图片代理: {exc}")
            try:
                artwork = client.discover_proxy_artwork(
                    artwork_id,
                    max_pages=int(settings["max_proxy_pages"]),
                )
            except PixivError as proxy_exc:
                log_warning(f"作品 {artwork_id} 下载失败: {proxy_exc}")
                counter["failed_artworks"] += 1
                return

        try:
            download_artwork(
                client,
                artwork,
                root,
                settings=settings,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
                metadata_only=args.metadata_only,
                quiet=args.quiet,
                counter=counter,
                counter_lock=threading.Lock(),
            )
            counter["artworks"] += 1
        except (PixivError, OSError) as exc:
            log_warning(f"作品 {artwork_id} 下载失败: {exc}")
            counter["failed_artworks"] += 1

    try:
        for kind, item_id, original_link in parsed_items:
            if kind == "artwork":
                process_artwork(item_id)
                continue

            if not settings.get("all_user_works", True):
                log_warning(f"作者链接已忽略（作者全作品功能关闭）: {original_link}")
                continue
            try:
                if client.api_disabled_reason:
                    raise PixivError(client.api_disabled_reason)
                work_ids = client.get_user_works(item_id)
            except PixivError as exc:
                log_warning(
                    f"作者 {item_id} 的作品列表获取失败: {exc}。"
                    "作者全部作品需要 Pixiv 元数据接口，请配置代理。"
                )
                counter["failed_artworks"] += 1
                continue

            if limit_works:
                work_ids = work_ids[:limit_works]
            log(f"[作者 {item_id}] 找到 {len(work_ids)} 件作品。")
            for artwork_id in work_ids:
                process_artwork(artwork_id)
    except KeyboardInterrupt:
        print("\n用户中断。已完成的内容会保留，可再次运行续传。", file=sys.stderr)
        return 130

    print_summary(counter)
    return 1 if counter["failed_artworks"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
