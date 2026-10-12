"""Crawl4AI 采集与不可变快照（设计 4.3 步骤 1-3、A1.2）。"""
import hashlib
import re
from urllib.parse import urlparse

from . import config, db as db_mod

# 验证码 / 访问限制 / 错误页特征：不能被当成"信息已删除"（设计 4.3-3）
BLOCKED_MARKERS = [
    "captcha", "are you a robot", "verify you are human", "attention required",
    "just a moment", "access denied", "403 forbidden", "404 not found",
    "page not found", "enable javascript",
]


class CrawlError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def validate_source(source):
    parsed = urlparse(source["url"])
    if parsed.scheme != "https" or parsed.hostname != source["allowed_host"]:
        raise CrawlError("config_error", "URL 必须为 HTTPS 且位于登记主机")
    if source.get("review_status") != "approved":
        raise CrawlError("config_error", "来源未获批准（review_status != approved）")
    if not source.get("enabled"):
        raise CrawlError("config_error", "来源已停用")


def normalize(text):
    return " ".join(text.split())


def page_fingerprint(markdown):
    return hashlib.sha256(normalize(markdown).encode()).hexdigest()


def detect_blocked_page(markdown):
    head = normalize(markdown).casefold()[:2000]
    for marker in BLOCKED_MARKERS:
        if marker in head:
            if marker in ("captcha", "are you a robot", "verify you are human", "attention required", "just a moment"):
                return "captcha"
            if marker == "access denied" or marker == "403 forbidden":
                return "access_denied"
            return "upstream_error"
    return None


def extract_markdown(result):
    markdown = result.markdown
    return markdown.raw_markdown if hasattr(markdown, "raw_markdown") else str(markdown or "")


async def fetch_page(source):
    """仅负责浏览器抓取，返回 result 与错误分类由调用方处理。"""
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        check_robots_txt=True,
        page_timeout=45000,
        wait_until="domcontentloaded",
        delay_before_return_html=2 if source.get("dynamic") else 0,
        css_selector=source.get("content_selector"),
    )
    async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as crawler:
        return await crawler.arun(url=source["url"], config=run_config)


async def crawl(source):
    """采集一个来源并写入不可变快照。返回快照信息；失败抛 CrawlError(kind, msg)。"""
    config.require_legacy_environment()
    config.PATHS.validate()
    validate_source(source)
    result = await fetch_page(source)
    if not result.success:
        message = result.error_message or "Crawl4AI returned success=false"
        kind = "access_denied" if re.search(r"robot|forbidden|403", message, re.I) else "upstream_error"
        raise CrawlError(kind, message)
    final_url = getattr(result, "url", source["url"])
    if urlparse(final_url).hostname != source["allowed_host"]:
        raise CrawlError("upstream_error", f"重定向到登记主机之外：{final_url}")
    markdown = extract_markdown(result)
    html = result.html or ""
    if len(normalize(markdown)) < 200:
        raise CrawlError("parse_failed", "抓取内容过短，疑似空白页或解析失败")
    blocked = detect_blocked_page(markdown)
    if blocked:
        raise CrawlError(blocked, f"页面呈现{blocked}特征，未按有效内容处理")
    page_hash = page_fingerprint(markdown)
    snapshot_dir = config.SNAPSHOT_DIR / source["source_id"]
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    html_path = snapshot_dir / f"{page_hash}.html"
    md_path = snapshot_dir / f"{page_hash}.md"
    # 不可变：同指纹文件只写一次，不覆盖历史
    if not html_path.exists():
        html_path.write_text(html, encoding="utf-8")
    if not md_path.exists():
        md_path.write_text(markdown, encoding="utf-8")
    with db_mod.session() as db:
        is_new = db_mod.insert_snapshot(db, page_hash, source["source_id"], final_url, html_path, md_path)
    return {
        "page_hash": page_hash,
        "final_url": final_url,
        "markdown": markdown,
        "html": html,
        "html_path": str(html_path),
        "markdown_path": str(md_path),
        "is_new_snapshot": is_new,
        "markdown_chars": len(markdown),
    }
