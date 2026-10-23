"""Anonymous, fixed-origin reads. No cookies, proxies, redirects or automatic retries."""
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
from urllib.parse import urlsplit, parse_qs, urlencode
from .common import Problem

MAX_JSON = 16 * 1024 * 1024
ALLOW_PROXY_FAKE_IP = False  # Local operator opt-in, never a browser-controlled URL option.


def parse_source(value, page):
    if type(page) is not int or not 1 <= page <= 1000:
        raise Problem("SELECT_PART", "必须明确选择 1—1000 范围内的一 P")
    if re.fullmatch(r"BV[A-Za-z0-9]{10}", value):
        bvid = value
    else:
        url = urlsplit(value)
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10})/?", url.path)
        if (url.scheme != "https" or url.netloc not in {"www.bilibili.com", "bilibili.com"}
                or not match or len(value) > 2048):
            raise Problem("UNSUPPORTED_SOURCE", "仅支持完整 B站 https 视频链接或 BV 号；短链接尚不支持")
        bvid = match[1]
        selected = parse_qs(url.query).get("p")
        if selected and selected != [str(page)]:
            raise Problem("PART_MISMATCH", "链接中的分 P 与明确选择不一致")
    return {"bvid": bvid, "page": page,
            "url": f"https://www.bilibili.com/video/{bvid}/?p={page}"}


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        addresses = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        def permitted(value):
            address = ipaddress.ip_address(value)
            return address.is_global or (ALLOW_PROXY_FAKE_IP and address.version == 4 and
                                         address in ipaddress.ip_network("198.18.0.0/15"))
        if not addresses or any(not permitted(a[4][0]) for a in addresses):
            raise Problem("UNSAFE_ADDRESS", "来源域名解析到非公开地址，已拒绝访问")
        family, kind, proto, _, address = addresses[0]
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(self.timeout)
            sock.connect(address)  # Use the already validated address; no second DNS lookup.
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def safe_url(value, kind):
    if value.startswith("//"):
        value = "https:" + value
    u = urlsplit(value)
    if (len(value) > 4096 or not value.isascii() or any(c in value for c in "\r\n\\")
            or u.scheme != "https" or u.fragment or u.username or u.password or u.port):
        raise Problem("UNSAFE_ADDRESS", "来源地址未通过校验")
    if kind == "api":
        good = u.netloc == "api.bilibili.com" and u.path in {
            "/x/web-interface/view", "/x/player/v2", "/x/player/playurl"}
    elif kind == "subtitle":
        good = (u.netloc == "aisubtitle.hdslb.com" and
                bool(re.fullmatch(r"/bfs/(?:subtitle/[A-Za-z0-9_./-]+\.json|ai_subtitle/prod/[A-Za-z0-9]{20,256})", u.path))
                and ".." not in u.path)
    elif kind == "audio":
        good = (bool(re.fullmatch(r"[A-Za-z0-9.-]+\.bilivideo\.com", u.netloc)) and
                bool(re.fullmatch(r"/[A-Za-z0-9_./-]+\.m4s", u.path)) and ".." not in u.path)
    else:
        good = False
    if not good:
        raise Problem("UNSAFE_ADDRESS", "来源不在受支持地址范围内")
    return u


def get_json(url, kind="api"):
    u = safe_url(url, kind)
    connection = PinnedHTTPS(u.hostname, timeout=20, context=ssl.create_default_context())
    try:
        connection.request("GET", u.path + ("?" + u.query if u.query else ""), headers={
            "User-Agent": "Mozilla/5.0 YuScribe/0.1", "Referer": "https://www.bilibili.com/",
            "Accept-Encoding": "identity"})
        response = connection.getresponse()
        if response.status in (401, 403):
            raise Problem("ACCESS_DENIED", "平台拒绝访问，可能需要登录或受到访问限制；未使用凭据")
        if response.status == 429 or response.status == 412:
            raise Problem("RATE_LIMITED", "平台访问限制，请稍后手动重试")
        if response.status != 200:
            raise Problem("HTTP_ERROR", f"平台返回 HTTP {response.status}；未跟随跳转")
        raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise Problem("RESPONSE_LIMIT", "平台返回内容超过 16MiB 上限")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except Problem:
        raise
    except (OSError, http.client.HTTPException):
        raise Problem("NETWORK_ERROR", "网络请求未完成；没有自动重试") from None
    except (ValueError, UnicodeError):
        raise Problem("INVALID_RESPONSE", "平台返回的 JSON 无效") from None
    finally:
        connection.close()


def api(path, params, fetch=get_json):
    payload = fetch("https://api.bilibili.com" + path + "?" + urlencode(params))
    code = payload.get("code")
    if code in (-101, -111):
        raise Problem("LOGIN_REQUIRED", "平台明确要求登录；首版未读取登录凭据")
    if code in (-352, -412, -509, -799):
        raise Problem("RATE_LIMITED", "平台风控限制；未自动重试或绕过")
    if code in (-404, 62002, 62012):
        raise Problem("SOURCE_UNAVAILABLE", "视频已失效、不可见或不存在")
    if code != 0 or not isinstance(payload.get("data"), dict):
        raise Problem("PLATFORM_ERROR", f"平台接口未成功（代码 {code if type(code) is int else 'unknown'}）")
    return payload["data"]


def inspect(source, fetch=get_json):
    data = api("/x/web-interface/view", {"bvid": source["bvid"]}, fetch)
    if data.get("bvid") != source["bvid"]:
        raise Problem("IDENTITY_MISMATCH", "平台视频身份与请求不一致")
    pages = data.get("pages") or []
    matches = [p for p in pages if p.get("page") == source["page"]]
    if len(matches) != 1 or type(matches[0].get("cid")) is not int or matches[0]["cid"] <= 0:
        raise Problem("SELECT_PART", "所选分 P 不存在或身份无效")
    part = matches[0]
    return {**source, "source_id": f"bilibili:{source['bvid']}:{part['cid']}",
            "cid": part["cid"], "title": data.get("title"), "author": (data.get("owner") or {}).get("name"),
            "author_id": (data.get("owner") or {}).get("mid"), "part_title": part.get("part"),
            "duration_seconds": part.get("duration"), "part_count": len(pages),
            "scope": "selected_part_only"}


def tracks(source, fetch=get_json):
    player = api("/x/player/v2", {"bvid": source["bvid"], "cid": source["cid"]}, fetch)
    # Missing response IDs are explicitly recorded as unverified, not inferred from duration.
    for name in ("bvid", "cid"):
        if name in player and str(player[name]) != str(source[name]):
            raise Problem("IDENTITY_MISMATCH", "字幕接口返回了其他视频或分 P")
    subtitle = player.get("subtitle") or {}
    rows = subtitle.get("subtitles") or []
    if not rows:
        if player.get("need_login_subtitle"):
            raise Problem("LOGIN_REQUIRED", "平台提示字幕需要登录；未使用凭据")
        raise Problem("NO_SUBTITLES", "当前匿名访问未返回字幕；不代表视频一定没有字幕")
    return rows, {"request_bvid": source["bvid"], "request_cid": source["cid"],
                  "response_bvid": player.get("bvid"), "response_cid": player.get("cid"),
                  "verification": "response_ids" if all(k in player for k in ("bvid", "cid")) else "request_binding_only"}


def extract(source, language, fetch=get_json):
    rows, binding = tracks(source, fetch)
    selected = [r for r in rows if r.get("lan") == language]
    if len(selected) != 1:
        available = ", ".join(str(r.get("lan", "unknown"))[:32] for r in rows[:16])
        raise Problem("LANGUAGE_UNAVAILABLE", f"所选字幕语言未唯一匹配；可用语言：{available}")
    track = selected[0]
    raw = fetch(track.get("subtitle_url", ""), "subtitle")
    segments = validate_cues(raw.get("body"))
    # ai- language prefix and explicit AI flag are evidence; absence is not human authorship proof.
    auto = language.startswith("ai-") or track.get("ai_type") in (1, 2)
    origin = "platform_auto_subtitle" if auto else "platform_subtitle_unknown_authorship"
    record = {"language": language, "track_id": str(track.get("id_str", track.get("id", "unknown"))),
              "origin": origin, "binding": binding, "model": None,
              "warnings": ["请核对原视频；请求绑定与接口返回不能证明字幕逐句准确"]}
    if binding["verification"] != "response_ids":
        record["warnings"].append("返回数据未同时给出 BV 与 CID，归属仅由受控请求链绑定，待人工核对")
    return raw, segments, record


def validate_cues(body):
    if not isinstance(body, list) or not 1 <= len(body) <= 100000:
        raise Problem("INVALID_SUBTITLE", "字幕为空或超过 100000 段上限")
    result, previous = [], -1.0
    for cue in body:
        start, end, text = cue.get("from"), cue.get("to"), cue.get("content")
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end) or start < previous
                or start < 0 or end < start or not isinstance(text, str) or not text.strip() or len(text) > 20000):
            raise Problem("INVALID_TIMING", "字幕分段或时间顺序无效；已保留失败状态")
        result.append({"start": start, "end": end, "text": text, "timing": "platform_cue"})
        previous = start
    return result


def download_audio(source, target, fetch=get_json):
    data = api("/x/player/playurl", {"bvid": source["bvid"], "cid": source["cid"],
                                   "fnval": 16, "qn": 16, "fourk": 0}, fetch)
    audio = (data.get("dash") or {}).get("audio") or []
    if not audio:
        raise Problem("AUDIO_UNAVAILABLE", "当前分 P 未返回可用独立音轨")
    track = min(audio, key=lambda r: int(r.get("bandwidth") or 10**9))
    u = safe_url(track.get("baseUrl") or track.get("base_url") or "", "audio")
    connection = PinnedHTTPS(u.hostname, timeout=20, context=ssl.create_default_context())
    try:
        connection.request("GET", u.path + ("?" + u.query if u.query else ""), headers={
            "User-Agent": "Mozilla/5.0", "Referer": source["url"], "Accept-Encoding": "identity"})
        response = connection.getresponse()
        if response.status != 200:
            raise Problem("AUDIO_ACCESS_DENIED", f"音轨返回 HTTP {response.status}；未自动重试或跟随跳转")
        size = 0
        with target.open("xb") as stream:
            while block := response.read(256 * 1024):
                size += len(block)
                if size > 256 * 1024**2:
                    raise Problem("MEDIA_LIMIT", "音轨超过256MiB上限，已停止")
                stream.write(block)
        if size == 0:
            raise Problem("AUDIO_EMPTY", "下载音轨为空")
        return {"audio_track_id": track.get("id"), "bytes": size,
                "binding": "playurl_request_bvid_cid", "duration_ms": data.get("timelength")}
    except (OSError, http.client.HTTPException):
        raise Problem("AUDIO_NETWORK_ERROR", "音轨下载未完成；临时文件不作为原稿") from None
    finally:
        connection.close()
