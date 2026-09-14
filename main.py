"""
AstrBot 抖音去水印下载插件

功能：
- 自动识别消息中的抖音链接
- 去水印下载视频
- 动图(slides)作品下载后转 MP4
- 多个动图片段拼接为一个 MP4
- 保留作品原始音乐

触发方式：发送包含抖音链接的消息即可自动触发。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import Plain, Video, Image

from .core import DouyinParser, DouyinDownloader, VideoConverter
from .core.parser import DouyinWorkInfo, MediaItem


# 抖音链接正则（用于 handler 注册）
_DOUYIN_REGEX = r"(https?://)?(v\.douyin\.com/[a-zA-Z0-9_\-]+|jx\.douyin\.com/[a-zA-Z0-9_\-]+|(?:www\.|m\.)?douyin\.com/(?:video|note|slides)/\d+|iesdouyin\.com/share/(?:slides|video|note)/\d+|aweme_id[=:/\s]+\d{10,}|aweme/\d{10,})"


class DouyinDLPlugin(Star):
    """抖音去水印下载插件入口"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config

        # 插件数据目录
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_douyin_nahida")
        self.download_dir = self.data_dir / "downloads"
        self.output_dir = self.data_dir / "output"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 用户配置的 cookie（可选，提升解析成功率）
        self.cookie: str = config.get("cookie", "")
        self.dynamic_mix_music: bool = config.get("dynamic_mix_music", True)

        # 各组件
        self.parser = DouyinParser(cookie=self.cookie)
        self.downloader = DouyinDownloader(download_dir=self.download_dir, cookie=self.cookie)
        self.converter = VideoConverter(output_dir=self.output_dir)

    async def terminate(self) -> None:
        """插件卸载时清理资源"""
        await self.parser.close()
        await self.downloader.close()

    @filter.regex(_DOUYIN_REGEX)
    async def on_douyin_link(self, event: AstrMessageEvent) -> None:
        """
        自动识别抖音链接并处理。

        当消息中包含抖音链接时自动触发。
        """
        text = event.get_message_str()
        # 提取抖音链接
        url = DouyinParser.find_douyin_url(text)
        if not url:
            return

        logger.info(f"[抖音] 收到链接: {url}")
        yield event.plain_result("🌿 收到抖音链接，正在解析去水印…")

        try:
            # 1. 解析作品信息
            work: DouyinWorkInfo = await self.parser.parse(url)
            logger.info(
                f"[抖音] 解析成功: {work.author} - {work.desc[:30]} "
                f"| 动图={work.is_dynamic} 视频={work.video_item is not None}"
            )

            # 2. 根据作品类型处理
            if work.is_dynamic:
                # 动图作品：下载所有片段 → 转码 → 拼接 → 合并音乐
                final_path = await self._process_dynamic_work(work)
                title = f"{work.author} - {work.desc}" if work.desc else work.author

                yield event.plain_result(
                    f"✅ 动图作品处理完成！\n📝 {title}\n🎬 {len(work.dynamic_items)} 个动图片段已拼接为 MP4"
                )
                yield event.chain_result([Video(file=str(final_path))])

            elif work.video_item:
                # 视频作品：下载后检测是否为图文（单张图片编译）
                final_path = await self._process_video_work(work)
                title = f"{work.author} - {work.desc}" if work.desc else work.author

                # 图文检测：抽两帧对比，差值极小则判定为单张图片
                is_image = await self.converter.detect_single_image(final_path)

                if is_image and work.cover_url:
                    # 图文类型：下载封面图发送，删除编译视频
                    cover_path = await self.downloader.download(
                        work.cover_url, filename=f"cover_{work.aweme_id[-8:]}", ext="jpg"
                    )
                    final_path.unlink(missing_ok=True)
                    yield event.plain_result(
                        f"✅ 图文作品下载完成！\n📝 {title}\n📷 已提取封面图片"
                    )
                    yield event.chain_result([Image(file=str(cover_path))])
                    cover_path.unlink(missing_ok=True)
                else:
                    # 普通视频：直接发送
                    yield event.plain_result(
                        f"✅ 视频下载完成！\n📝 {title}"
                    )
                    yield event.chain_result([Video(file=str(final_path))])

            elif work.image_items:
                # 纯图片图集：直接下载并发送图片
                prefix = work.aweme_id[-8:] or uuid.uuid4().hex[:8]
                image_paths = await self.downloader.download_media_items(
                    work.image_items, prefix=f"img_{prefix}_"
                )
                if not image_paths:
                    yield event.plain_result("❌ 图片下载失败")
                    return

                title = f"{work.author} - {work.desc}" if work.desc else work.author
                yield event.plain_result(
                    f"✅ 图集下载完成！\n📝 {title}\n📷 共 {len(image_paths)} 张图片"
                )
                yield event.chain_result([Image(file=str(p)) for p in image_paths])

                for p in image_paths:
                    p.unlink(missing_ok=True)

            else:
                yield event.plain_result("❌ 未能提取到可下载的媒体内容")

        except Exception as e:
            logger.exception(f"[抖音] 处理失败: {e}")
            yield event.plain_result(f"❌ 处理失败: {e}")

    async def _process_video_work(self, work: DouyinWorkInfo) -> Path:
        """处理普通视频作品：下载无水印视频"""
        assert work.video_item is not None

        prefix = work.aweme_id[-8:] or uuid.uuid4().hex[:8]
        video_path = await self.downloader.download(
            work.video_item.url, filename=prefix, ext="mp4"
        )
        return video_path

    async def _process_dynamic_work(self, work: DouyinWorkInfo) -> Path:
        """
        处理动图作品：
        1. 下载所有动图片段
        2. 转码为统一 MP4
        3. 拼接
        4. 合并作品音乐
        """
        assert work.dynamic_items

        prefix = work.aweme_id[-8:] or uuid.uuid4().hex[:8]

        # 1. 下载所有动图片段
        yield_msg = None  # 在非生成器中无法 yield，用日志代替
        logger.info(f"[抖音] 开始下载 {len(work.dynamic_items)} 个动图片段")
        segment_paths = await self.downloader.download_media_items(
            work.dynamic_items, prefix=prefix
        )

        if not segment_paths:
            raise RuntimeError("所有动图片段下载失败")

        if len(segment_paths) < len(work.dynamic_items):
            logger.warning(
                f"[抖音] 部分片段下载失败: "
                f"{len(segment_paths)}/{len(work.dynamic_items)}"
            )

        # 2. 转码 + 拼接 + 合并音乐
        mix_music = self.dynamic_mix_music and bool(work.music_url)
        final = await self.converter.process_dynamic_work(
            segments=segment_paths,
            music_url=work.music_url or None,
            mix_music=mix_music,
        )

        # 3. 清理下载的临时片段
        for p in segment_paths:
            p.unlink(missing_ok=True)

        return final

    async def _process_image_slides(self, work: DouyinWorkInfo) -> Path:
        """
        处理幻灯片图集（静态图片 + 音乐）：
        1. 下载所有图片
        2. 每张图转短视频片段
        3. 按音乐时长均分，拼接
        4. 合并作品音乐
        """
        assert work.image_items

        prefix = work.aweme_id[-8:] or uuid.uuid4().hex[:8]
        logger.info(f"[抖音] 开始下载 {len(work.image_items)} 张图集图片")
        image_paths = await self.downloader.download_media_items(
            work.image_items, prefix=prefix
        )

        if not image_paths:
            raise RuntimeError("所有图片下载失败")

        final = await self.converter.process_image_slides(
            image_paths=image_paths,
            music_url=work.music_url,
            music_duration=work.music_duration,
        )

        for p in image_paths:
            p.unlink(missing_ok=True)

        return final
