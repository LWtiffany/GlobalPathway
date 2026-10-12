"""Conditional page checks and complete, stable Markdown section planning."""
import hashlib
import re
from dataclasses import dataclass
from urllib import error, parse, request, robotparser


MAX_PROBE_BYTES = 5_000_000
MAX_SECTION_CHARS = 3500
_HEADING = re.compile(r"(?m)^#{1,4}\s+(.+?)\s*$")
_CROSS_SECTION = re.compile(
    r"\b(unless|except|exemption|exempt|eligib\w*|do not need|without proof|"
    r"if you have|if you are|when you|financial sponsorship)\b|豁免|除非|条件|资格", re.I)


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalized_hash(text):
    return fingerprint(" ".join(text.split()))


def capture_config_hash(source):
    import json
    relevant = {key: source.get(key) for key in
                ("url", "allowed_host", "content_selector", "dynamic", "schema_type")}
    return fingerprint(json.dumps(relevant, sort_keys=True, ensure_ascii=False))


@dataclass(frozen=True)
class Section:
    key: str
    heading: str
    text: str
    start: int
    end: int
    content_hash: str


def _chunks(text, limit):
    """Cover every input character, including oversized paragraphs, with overlap."""
    if len(text) <= limit:
        return [(0, text)]
    chunks = []
    pos = 0
    while pos < len(text):
        end = min(pos + limit, len(text))
        if end < len(text):
            boundary = text.rfind("\n", pos + limit // 2, end)
            if boundary < 0:
                boundary = text.rfind(" ", pos + limit // 2, end)
            if boundary > pos:
                end = boundary + 1
        chunks.append((pos, text[pos:end]))
        if end == len(text):
            break
        pos = max(pos + 1, end - 160)
    return chunks


def split_sections(markdown, limit=MAX_SECTION_CHARS):
    if not markdown.strip():
        raise ValueError("正文为空，不能标记抽取覆盖完整")
    matches = list(_HEADING.finditer(markdown))
    bounds = []
    if not matches:
        bounds.append(("page", 0, len(markdown)))
    else:
        if matches[0].start() > 0:
            bounds.append(("preamble", 0, matches[0].start()))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
            bounds.append((match.group(1).strip(), match.start(), end))
    seen = {}
    sections = []
    for heading, start, end in bounds:
        body = markdown[start:end]
        if not body.strip():
            continue
        name = re.sub(r"\s+", " ", heading).casefold()
        seen[name] = seen.get(name, 0) + 1
        base = f"{fingerprint(name)[:12]}-{seen[name]}"
        for part, (offset, chunk) in enumerate(_chunks(body, limit), 1):
            key = f"{base}-{part}"
            sections.append(Section(key, heading, chunk, start + offset,
                                    start + offset + len(chunk), normalized_hash(heading + "\n" + chunk)))
    if not sections:
        raise ValueError("正文没有可抽取章节")
    # Guard against accidentally dropping the end of an oversized section.
    if sections[-1].end != len(markdown):
        raise ValueError("章节规划没有覆盖正文末尾")
    return sections


def changed_sections(previous, current, dependencies=()):
    """Return changed keys, removed keys and conservative dependent invalidations."""
    old = {section.key: section for section in previous}
    new = {section.key: section for section in current}
    changed = {key for key, section in new.items()
               if key not in old or old[key].content_hash != section.content_hash}
    removed = set(old) - set(new)
    affected = changed | removed
    for key, dependency in dependencies:
        if dependency in affected and key in new:
            changed.add(key)
    # A changed eligibility/exception section can change the meaning of every amount.
    if any(_CROSS_SECTION.search((new.get(key) or old[key]).text) for key in affected):
        changed = set(new)
    return changed, removed


@dataclass(frozen=True)
class Probe:
    result: str  # NOT_MODIFIED, BODY_UNCHANGED, CHANGED, UNAVAILABLE
    etag: str | None = None
    last_modified: str | None = None
    raw_body_hash: str | None = None
    detail: str | None = None


class BlockedByRobots(Exception):
    pass


class _SameHostRedirect(request.HTTPRedirectHandler):
    def __init__(self, host):
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = parse.urlparse(newurl)
        if target.scheme != "https" or target.hostname != self.host:
            raise ValueError("条件请求重定向到未登记主机")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def conditional_probe(source, state, timeout=20):
    """Static pages only. Robots check precedes the lightweight conditional GET."""
    if source.get("dynamic"):
        return Probe("UNAVAILABLE", detail="dynamic_page")
    url = source["url"]
    parsed = parse.urlparse(url)
    host = source["allowed_host"]
    if parsed.scheme != "https" or parsed.hostname != host:
        raise ValueError("来源 URL 必须为登记主机的 HTTPS 地址")
    opener = request.build_opener(_SameHostRedirect(host))
    agent = "GlobalPathwayAgent1/1.0"
    robots_url = f"https://{host}/robots.txt"
    try:
        try:
            with opener.open(request.Request(robots_url, headers={"User-Agent": agent}), timeout=timeout) as response:
                robots_text = response.read(500_001)
                if len(robots_text) > 500_000:
                    return Probe("UNAVAILABLE", detail="robots_too_large")
                parser = robotparser.RobotFileParser()
                parser.parse(robots_text.decode("utf-8", errors="replace").splitlines())
        except error.HTTPError as exc:
            if exc.code != 404:
                return Probe("UNAVAILABLE", detail=f"robots_http_{exc.code}")
            parser = robotparser.RobotFileParser()
            parser.parse([])
        if not parser.can_fetch(agent, url):
            raise BlockedByRobots("来源 robots.txt 不允许轻量检查")
        headers = {"User-Agent": agent}
        if state.get("etag"):
            headers["If-None-Match"] = state["etag"]
        if state.get("last_modified"):
            headers["If-Modified-Since"] = state["last_modified"]
        try:
            response = opener.open(request.Request(url, headers=headers), timeout=timeout)
        except error.HTTPError as exc:
            if exc.code == 304 and state.get("snapshot_id"):
                return Probe("NOT_MODIFIED", state.get("etag"), state.get("last_modified"),
                             state.get("raw_body_hash"))
            return Probe("UNAVAILABLE", detail=f"source_http_{exc.code}")
        with response:
            if parse.urlparse(response.url).hostname != host:
                raise ValueError("条件请求响应来自未登记主机")
            body = response.read(MAX_PROBE_BYTES + 1)
            if len(body) > MAX_PROBE_BYTES:
                return Probe("UNAVAILABLE", detail="body_too_large")
            body_hash = hashlib.sha256(body).hexdigest()
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
            if body_hash == state.get("raw_body_hash") and state.get("snapshot_id"):
                return Probe("BODY_UNCHANGED", etag, last_modified, body_hash)
            return Probe("CHANGED", etag, last_modified, body_hash)
    except (OSError, TimeoutError) as exc:
        return Probe("UNAVAILABLE", detail=type(exc).__name__)
