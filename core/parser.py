"""
抖音链接解析器

负责：
1. 从消息中匹配抖音链接
2. 短链重定向获取真实 URL
3. 通过 f2 ABogus 签名调网页详情 API 提取实况照片/Live Photo 动图数据

技术参考自 astrbot_plugin_parser 的 DouyinParser，但自包含、去耦合。
"""

from __future__ import annotations
from astrbot.api import logger

import re
import json
import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import aiohttp


# f2 ABogus 签名（按需导入，不影响无 f2 环境的基础功能）
try:
    from f2.apps.douyin.utils import ABogusManager
    _HAS_F2 = True
except ImportError:
    _HAS_F2 = False
    logger.warning("f2 库未安装，网页详情 API 签名不可用，将回退到 slidesinfo API")

# ---------- 常量 ----------

IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/101.0.4951.61 Mobile Safari/537.36"
)
WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# f2 自带的测试 cookie（含 UIFID/ttwid）
F2_TEST_YAML = "/usr/local/lib/python3.12/site-packages/f2/conf/test.yaml"
WEB_DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
PLAY_RATIOS = ("1080p", "720p", "540p", "360p")
SLIDES_API = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"

# 抖音链接匹配正则
DOUYIN_PATTERNS = [
    re.compile(r"v\.douyin\.com/[a-zA-Z0-9_\-]+"),
    re.compile(r"jx\.douyin\.com/[a-zA-Z0-9_\-]+"),
    re.compile(r"(?:www\.|m\.)?douyin\.com/(?:video|note|slides)/(?P<vid>\d+)"),
    re.compile(r"iesdouyin\.com/share/(?:slides|video|note)/(?P<vid>\d+)"),
    re.compile(r"aweme_id[=:/\s]+(?P<vid>\d{10,})"),
    re.compile(r"aweme/(?P<vid>\d{10,})"),
    re.compile(r"(?<![A-Za-z0-9_/=:%?&.-])(?P<vid>\d{18,20})(?!\d)"),
]


# ---------- 数据结构 ----------


@dataclass
class MediaItem:
    """单个媒体项（视频或动图片段）"""

    url: str
    # 该项是否已包含音频轨
    has_audio: bool = False
    # 原始类型提示：video / dynamic / image
    kind: str = "video"


@dataclass
class DouyinWorkInfo:
    """解析后的作品信息"""

    aweme_id: str
    desc: str = ""
    author: str = ""
    # 视频作品（普通视频）
    video_item: MediaItem | None = None
    # 动图作品（slides 中的动态图片列表）
    dynamic_items: list[MediaItem] = field(default_factory=list)
    # 静态图片列表（图集但无动态效果）
    image_items: list[MediaItem] = field(default_factory=list)
    # 作品音乐
    music_url: str = ""
    music_title: str = ""
    music_duration: float = 0.0
    # 封面图 URL（图文类型用）
    cover_url: str = ""
    # 是否为幻灯片图集（图片+音乐，需图片转视频后拼接）
    is_slides_album: bool = False
    create_time: int = 0
    # 原始 raw 数据，调试用
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_slides(self) -> bool:
        """是否为动图/图集作品"""
        return bool(self.dynamic_items) or bool(self.image_items)

    @property
    def is_dynamic(self) -> bool:
        """是否包含动图（有视频的动态图片）"""
        return bool(self.dynamic_items)

    @property
    def needs_image_to_video(self) -> bool:
        """是否需要将静态图片转为视频（幻灯片图集）"""
        return self.is_slides_album and bool(self.image_items)


# ---------- 解析器 ----------


class DouyinParser:
    """抖音链接解析器（异步）"""

    def __init__(self, cookie: str = "") -> None:
        self.cookie = cookie
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "User-Agent": IOS_UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            if self.cookie:
                headers["Cookie"] = self.cookie
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ===== 公开方法 =====

    @staticmethod
    def find_douyin_url(text: str) -> str | None:
        """从消息文本中提取第一个抖音链接"""
        for pat in DOUYIN_PATTERNS:
            m = pat.search(text)
            if m:
                matched = m.group(0)
                # 纯数字 ID 的情况，补充前缀
                if matched.replace(".", "").replace("/", "").isdigit():
                    return f"https://www.douyin.com/video/{matched}"
                if not matched.startswith("http"):
                    return f"https://{matched}"
                return matched
        return None

    @staticmethod
    def extract_aweme_id(url: str) -> str | None:
        """从 URL 中提取 aweme_id"""
        for pat in DOUYIN_PATTERNS:
            m = pat.search(url)
            if m and "vid" in m.groupdict():
                return m.group("vid")
        # 短链需要重定向后才有 ID
        return None

    async def parse(self, url: str) -> DouyinWorkInfo:
        """
        解析抖音链接，返回作品信息。

        流程：
        1. 短链 → 重定向获取真实 URL
        2. 提取 aweme_id
        3. 尝试 slides API（动图/图集）
        4. 尝试 share 页面 _ROUTER_DATA（视频/图集）
        """
        # 1. 重定向短链
        resolved = await self._resolve_short_link(url)
        logger.info(f"[抖音] 解析链接: {url} → {resolved}")

        # 2. 提取 ID
        aweme_id = self.extract_aweme_id(resolved) or self.extract_aweme_id(url)
        if not aweme_id:
            raise ValueError(f"无法从链接提取作品ID: {url}")

        # 3. 优先使用 f2 签名的网页详情 API（完整 Live Photo 数据）
        if _HAS_F2:
            try:
                info = await self._parse_web_detail(aweme_id)
                if info is not None:
                    logger.info(f"[抖音] 网页详情API命中: {info.desc[:40]}")
                    return info
            except Exception as e:
                logger.warning(f"[抖音] 网页详情API失败，回退: {e}")

        # 4. 回退: slides API（动图/图集专用，数据可能精简）
        try:
            info = await self._parse_slides_api(aweme_id)
            if info is not None:
                logger.info(f"[抖音] slides API 命中: {info.desc[:40]}")
                return info
        except Exception as e:
            logger.debug(f"[抖音] slides API 未命中或失败: {e}")

        # 5. 回退: share 页面 _ROUTER_DATA
        info = await self._parse_share_page(aweme_id)
        if info is not None:
            logger.info(f"[抖音] share 页面命中: {info.desc[:40]}")
            return info

        raise ValueError(f"解析失败，作品可能已删除或私密: {aweme_id}")

    async def probe_video_url(self, video_id: str, referer: str = "https://www.iesdouyin.com/") -> tuple[str, int]:
        """
        探测无水印视频直链。

        通过 aweme.snssdk.com/aweme/v1/play/ 端点（不带 playwm）获取无水印视频，
        遍历多个清晰度，返回文件最大的那个。

        Returns:
            (直链URL, 文件大小字节)
        """
        best_url = ""
        best_size = 0

        headers = {
            "User-Agent": IOS_UA,
            "Referer": referer,
            "Range": "bytes=0-1",
        }

        for ratio in PLAY_RATIOS:
            play_url = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={video_id}&ratio={ratio}&line=0"
            try:
                async with self.session.get(
                    play_url, headers=headers, allow_redirects=True
                ) as resp:
                    if resp.status >= 400:
                        continue
                    size = self._extract_size(resp.headers)
                    if size > best_size:
                        best_size = size
                        best_url = str(resp.url)
            except (aiohttp.ClientError, TimeoutError) as e:
                logger.debug(f"[抖音] 探测 ratio={ratio} 失败: {e}")
                continue

        if not best_url:
            raise ValueError(f"无法探测无水印直链: {video_id}")
        return best_url, best_size

    async def _parse_web_detail(self, aweme_id: str) -> DouyinWorkInfo | None:
        """
        通过 f2 ABogus 签名调用网页详情 API，获取完整作品数据。

        该 API 返回的 images 包含 video 字段（实况照片/Live Photo 动图），
        是获取动图数据的唯一可靠来源。

        由于 ArgusSecurityPlugin 的 uifid 验证不稳定，带 3 次重试。
        """
        if not _HAS_F2:
            return None

        # 加载 cookie（用户配置优先，否则用 f2 自带测试 cookie）
        cookie = self.cookie or self._load_f2_test_cookie()
        if not cookie:
            logger.warning("[抖音] 无可用 cookie，网页详情API将无法调用")
            return None

        params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": "4",
            "version_code": "170400",
            "version_name": "17.4.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Edge",
            "browser_version": "120.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "120",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": "12",
            "device_memory": "8",
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": "50",
            "aweme_id": aweme_id,
        }

        headers = {
            "User-Agent": WEB_UA,
            "Referer": "https://www.douyin.com/",
            "Cookie": cookie,
            "Accept": "application/json",
        }

        for attempt in range(3):
            endpoint = ABogusManager.model_2_endpoint(
                WEB_UA, WEB_DETAIL_URL, params
            )
            try:
                async with self.session.get(
                    endpoint, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status != 200:
                        logger.debug(f"[抖音] 网页详情API attempt={attempt} status={resp.status}")
                        await asyncio.sleep(1)
                        continue
                    text = await resp.text()
                    if not text:
                        await asyncio.sleep(1)
                        continue
                    data = json.loads(text)
            except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as e:
                logger.debug(f"[抖音] 网页详情API attempt={attempt} err={e}")
                await asyncio.sleep(1)
                continue

            aw = data.get("aweme_detail")
            if not aw:
                logger.warning("[抖音] 网页详情API返回但无 aweme_detail")
                return None

            return await self._build_from_web_detail(aw, aweme_id)

        logger.warning("[抖音] 网页详情API 3次重试均失败")
        return None

    @staticmethod
    def _load_f2_test_cookie() -> str:
        """从 f2 自带的 test.yaml 加载 cookie（含 UIFID/ttwid）"""
        try:
            import yaml
            with open(F2_TEST_YAML, encoding="utf-8") as f:
                conf = yaml.safe_load(f)
            return conf.get("douyin", {}).get("cookie", "")
        except Exception:
            return ""

    async def _build_from_web_detail(self, item: dict[str, Any], aweme_id: str) -> DouyinWorkInfo:
        """从网页详情 API 响应构建 WorkInfo（含 Live Photo 动图数据）"""
        desc = item.get("desc", "")
        author = (item.get("author") or {}).get("nickname", "")
        create_time = item.get("create_time", 0)
        images = item.get("images") or []

        dynamic_items: list[MediaItem] = []
        image_items: list[MediaItem] = []

        for img in images:
            # 实况照片/Live Photo: image 含有 video 字段
            vid = img.get("video")
            if isinstance(vid, dict):
                play_addr = vid.get("play_addr") or {}
                urls = play_addr.get("url_list") or []
                if urls:
                    url = urls[0]
                    url = url.replace("playwm", "play")
                    dynamic_items.append(
                        MediaItem(url=url, has_audio=False, kind="dynamic")
                    )
                    continue
            # 静态图片
            urls = img.get("url_list") or []
            if not urls:
                urls = img.get("download_url_list") or []
            if urls:
                image_items.append(MediaItem(url=urls[0], kind="image"))

        # 音乐
        music_url, music_title, music_duration = self._extract_music(item)

        # 普通视频作品
        video_item: MediaItem | None = None
        video = item.get("video")
        if isinstance(video, dict) and not images:
            play_addr = video.get("play_addr") or {}
            uri = play_addr.get("uri")
            urls = play_addr.get("url_list") or []

            video_url = None
            if uri:
                try:
                    probed_url, _ = await self.probe_video_url(uri, "https://www.douyin.com/")
                    video_url = probed_url
                except Exception as e:
                    logger.debug(f"[抖音] play 探测失败，回退 play_addr: {e}")
            if not video_url and urls:
                video_url = urls[0].replace("playwm", "play")
            if video_url:
                video_item = MediaItem(url=video_url, has_audio=True, kind="video")

        # 封面图 URL
        cover_url = ""
        if isinstance(video, dict):
            cover = video.get("cover") or {}
            cover_urls = cover.get("url_list") or []
            if cover_urls:
                cover_url = cover_urls[0]

        # 幻灯片图集判定（图片+音乐但无动图）
        is_slides_album = bool(image_items) and not dynamic_items and bool(music_url)

        return DouyinWorkInfo(
            aweme_id=aweme_id,
            desc=desc,
            author=author,
            video_item=video_item,
            dynamic_items=dynamic_items,
            image_items=image_items,
            music_url=music_url,
            music_title=music_title,
            music_duration=music_duration,
            cover_url=cover_url,
            is_slides_album=is_slides_album,
            create_time=create_time,
            raw=item,
        )

    # ===== 内部方法 =====

    async def _resolve_short_link(self, url: str) -> str:
        """重定向短链，返回最终 URL"""
        if "v.douyin.com" not in url and "jx.douyin.com" not in url:
            return url

        headers = {"User-Agent": IOS_UA}
        if self.cookie:
            headers["Cookie"] = self.cookie

        async with self.session.get(
            url, headers=headers, allow_redirects=False
        ) as resp:
            # 更新 cookie
            self._update_cookies(resp.headers)
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location", url)
                if loc and not loc.startswith("http"):
                    loc = "https://" + loc.lstrip("/")
                return loc or url
            return url

    def _update_cookies(self, headers: dict[str, Any]) -> None:
        """从响应头更新 cookie"""
        set_cookies = headers.getall("Set-Cookie", []) if hasattr(headers, "getall") else []
        if not set_cookies:
            return
        parts: list[str] = []
        for sc in set_cookies:
            kv = sc.split(";")[0].strip()
            if kv:
                parts.append(kv)
        if parts:
            existing = self.cookie
            self.cookie = existing + "; " + "; ".join(parts) if existing else "; ".join(parts)
            # 更新 session 头
            if self._session:
                self._session.headers["Cookie"] = self.cookie

    async def _ensure_ttwid(self) -> None:
        """确保拥有匿名 ttwid cookie（用于访问 iesdouyin 分享页）"""
        if "ttwid" in self.cookie:
            return

        headers = {
            "User-Agent": IOS_UA,
            "Content-Type": "application/json",
            "Referer": "https://www.iesdouyin.com/",
        }
        payload = {
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.iesdouyin.com",
            "union": True,
            "fid": "",
        }
        try:
            async with self.session.post(
                TTWID_REGISTER_URL, json=payload, headers=headers
            ) as resp:
                self._update_cookies(resp.headers)
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            logger.warning(f"[抖音] ttwid 注册失败: {e}")
            return

        # 回调 URL 获取完整 cookie
        if isinstance(body, dict) and (cb := body.get("redirect_url")):
            cb_headers = {"User-Agent": IOS_UA, "Referer": "https://www.iesdouyin.com/"}
            try:
                async with self.session.get(
                    cb, headers=cb_headers, allow_redirects=False
                ) as resp:
                    self._update_cookies(resp.headers)
            except (aiohttp.ClientError, TimeoutError):
                pass

    async def _parse_slides_api(self, aweme_id: str) -> DouyinWorkInfo | None:
        """调用 slidesinfo API 解析动图/图集作品"""
        headers = {"User-Agent": ANDROID_UA}
        if self.cookie:
            headers["Cookie"] = self.cookie

        params = {
            "aweme_ids": f"[{aweme_id}]",
            "request_source": "200",
        }
        try:
            async with self.session.get(
                SLIDES_API, params=params, headers=headers
            ) as resp:
                if resp.status != 200:
                    return None
                self._update_cookies(resp.headers)
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None

        details = data.get("aweme_details")
        if not details:
            # filter_list reason 存在说明不是 slides 类型
            return None

        item = details[0]
        return self._build_from_slides(item, aweme_id)

    async def _parse_share_page(self, aweme_id: str) -> DouyinWorkInfo | None:
        """通过分享页 _ROUTER_DATA 解析作品"""
        await self._ensure_ttwid()

        headers = {"User-Agent": IOS_UA}
        if self.cookie:
            headers["Cookie"] = self.cookie

        # 同时尝试 video 和 note 类型的分享页
        for ty in ("video", "note"):
            share_url = f"https://www.iesdouyin.com/share/{ty}/{aweme_id}/"
            try:
                async with self.session.get(
                    share_url, headers=headers, allow_redirects=False
                ) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
                    self._update_cookies(resp.headers)
            except (aiohttp.ClientError, TimeoutError):
                continue

            router = self._extract_router_data(text)
            if not router:
                continue

            item = self._get_item_from_router(router)
            if item:
                return await self._build_from_share(item, aweme_id, share_url)

        return None

    @staticmethod
    def _extract_router_data(html: str) -> dict[str, Any] | None:
        """从 HTML 中提取 window._ROUTER_DATA"""
        pat = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL)
        m = pat.search(html)
        if not m or not m.group(1):
            return None
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _get_item_from_router(router: dict[str, Any]) -> dict[str, Any] | None:
        """从 _ROUTER_DATA 中提取 item_list[0]"""
        loader = router.get("loaderData", {})
        for key, val in loader.items():
            if not isinstance(val, dict):
                continue
            vir = val.get("videoInfoRes")
            if isinstance(vir, dict):
                items = vir.get("item_list")
                if items:
                    return items[0]
            # slides 页面可能有 noteInfoRes
            nir = val.get("noteInfoRes")
            if isinstance(nir, dict):
                items = nir.get("item_list")
                if items:
                    return items[0]
        return None

    @staticmethod
    def _extract_music(item: dict[str, Any]) -> tuple[str, str, float]:
        """
        从作品数据提取音乐信息。

        优先 play_url.url_list，若为空（受限音乐）则回退
        music.extra 里的 original_song_url（原声直链）。

        Returns:
            (music_url, music_title, music_duration)
        """
        music = item.get("music") or {}
        if not isinstance(music, dict):
            return "", "", 0.0

        music_title = music.get("title", "")
        music_duration = float(
            music.get("duration") or music.get("audition_duration") or 0
        )

        # 1. 首选 play_url.url_list
        play_url = music.get("play_url") or {}
        if isinstance(play_url, dict):
            urls = play_url.get("url_list") or []
            if urls:
                return urls[0], music_title, music_duration

        # 2. 回退 extra.original_song_url（受限音乐时）
        extra = music.get("extra") or ""
        if extra:
            try:
                extra_data = json.loads(extra) if isinstance(extra, str) else extra
                song_url = extra_data.get("original_song_url", "")
                if song_url:
                    return song_url, music_title, music_duration
            except (json.JSONDecodeError, AttributeError):
                pass

        return "", music_title, music_duration

    def _build_from_slides(self, item: dict[str, Any], aweme_id: str) -> DouyinWorkInfo:
        """从 slides API 响应构建 WorkInfo"""
        desc = item.get("desc", "")
        author = (item.get("author") or {}).get("nickname", "")
        create_time = item.get("create_time", 0)
        images = item.get("images") or []

        dynamic_items: list[MediaItem] = []
        image_items: list[MediaItem] = []

        for img in images:
            # 动图：image 含有 video 字段
            vid = img.get("video")
            if isinstance(vid, dict):
                play_addr = vid.get("play_addr") or {}
                urls = play_addr.get("url_list") or []
                if urls:
                    url = urls[0]
                    # 去 playwm
                    url = url.replace("playwm", "play")
                    dynamic_items.append(
                        MediaItem(url=url, has_audio=False, kind="dynamic")
                    )
                    continue
            # 静态图片：优先 url_list[0]（无水印），回退 download_url_list
            urls = img.get("url_list") or []
            if not urls:
                urls = img.get("download_url_list") or []
            if urls:
                image_items.append(MediaItem(url=urls[0], kind="image"))

        # 音乐信息
        music_url = ""
        music_title = ""
        music_duration = 0.0
        music = item.get("music") or {}
        if isinstance(music, dict):
            play_url = music.get("play_url") or {}
            if isinstance(play_url, dict):
                music_urls = play_url.get("url_list") or []
                if music_urls:
                    music_url = music_urls[0]
            music_title = music.get("title", "")
            music_duration = float(music.get("duration") or music.get("audition_duration") or 0)

        # 判断是否为幻灯片图集（图片+音乐，需要图片转视频）
        is_slides_album = bool(image_items) and not dynamic_items and bool(music_url)

        return DouyinWorkInfo(
            aweme_id=aweme_id,
            desc=desc,
            author=author,
            dynamic_items=dynamic_items,
            image_items=image_items,
            music_url=music_url,
            music_title=music_title,
            music_duration=music_duration,
            is_slides_album=is_slides_album,
            create_time=create_time,
            raw=item,
        )

    async def _build_from_share(
        self, item: dict[str, Any], aweme_id: str, share_url: str
    ) -> DouyinWorkInfo:
        """从分享页 _ROUTER_DATA 构建 WorkInfo"""
        desc = item.get("desc", "")
        author = (item.get("author") or {}).get("nickname", "")
        create_time = item.get("create_time", 0)

        video_item: MediaItem | None = None
        dynamic_items: list[MediaItem] = []
        image_items: list[MediaItem] = []

        video = item.get("video")
        images = item.get("images")

        # 图集/动图
        if images:
            for img in images:
                vid = img.get("video")
                if isinstance(vid, dict):
                    play_addr = vid.get("play_addr") or {}
                    urls = play_addr.get("url_list") or []
                    if urls:
                        url = urls[0].replace("playwm", "play")
                        dynamic_items.append(
                            MediaItem(url=url, has_audio=False, kind="dynamic")
                        )
                        continue
                urls = img.get("url_list") or []
                if urls:
                    image_items.append(MediaItem(url=urls[0], kind="image"))

        # 普通视频（有图片集时不处理，避免图集被误判为视频）
        if isinstance(video, dict) and not images:
            play_addr = video.get("play_addr") or {}
            uri = play_addr.get("uri")
            urls = play_addr.get("url_list") or []

            video_url = None
            # 优先用 play 端点探测无水印直链
            if uri:
                try:
                    probed_url, _ = await self.probe_video_url(uri, share_url)
                    video_url = probed_url
                except Exception as e:
                    logger.debug(f"[抖音] play 探测失败，回退 play_addr: {e}")

            if not video_url and urls:
                video_url = urls[0].replace("playwm", "play")

            if video_url:
                video_item = MediaItem(url=video_url, has_audio=True, kind="video")

        music_url = ""
        music_title = ""
        music = item.get("music") or {}
        if isinstance(music, dict):
            play_url = music.get("play_url") or {}
            music_urls = play_url.get("url_list") or []
            if music_urls:
                music_url = music_urls[0]
            music_title = music.get("title", "")

        # 封面图 URL
        cover_url = ""
        if isinstance(video, dict):
            cover = video.get("cover") or {}
            cover_urls = cover.get("url_list") or []
            if cover_urls:
                cover_url = cover_urls[0]

        return DouyinWorkInfo(
            aweme_id=aweme_id,
            desc=desc,
            author=author,
            video_item=video_item,
            dynamic_items=dynamic_items,
            image_items=image_items,
            music_url=music_url,
            music_title=music_title,
            cover_url=cover_url,
            create_time=create_time,
            raw=item,
        )

    @staticmethod
    def _extract_size(headers: Any) -> int:
        """从响应头提取文件大小"""
        # Content-Range: bytes 0-1/12345678
        cr = headers.get("Content-Range")
        if cr:
            m = re.search(r"/(\d+)\s*$", cr)
            if m:
                return int(m.group(1))
        cl = headers.get("Content-Length")
        if cl:
            try:
                return int(cl)
            except ValueError:
                return 0
        return 0
