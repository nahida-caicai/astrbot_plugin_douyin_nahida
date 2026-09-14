"""
抖音媒体下载器

负责将解析得到的媒体 URL 下载到本地临时文件。
"""

from __future__ import annotations
from astrbot.api import logger

import asyncio
import os
import uuid
from pathlib import Path

import aiohttp

from .parser import IOS_UA, MediaItem


# 默认下载目录
DEFAULT_DOWNLOAD_DIR = Path("/tmp") / "douyin_nahida"


class DouyinDownloader:
    """异步媒体下载器"""

    def __init__(self, download_dir: str | Path | None = None, cookie: str = "") -> None:
        self.download_dir = Path(download_dir or DEFAULT_DOWNLOAD_DIR)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.cookie = cookie
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "User-Agent": IOS_UA,
                "Referer": "https://www.douyin.com/",
            }
            if self.cookie:
                headers["Cookie"] = self.cookie
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def download(
        self,
        url: str,
        filename: str | None = None,
        ext: str = "mp4",
        max_size: int = 500 * 1024 * 1024,  # 500MB
    ) -> Path:
        """
        下载指定 URL 到本地文件。

        Args:
            url: 媒体直链
            filename: 文件名（不含扩展名），不填则随机
            ext: 扩展名
            max_size: 最大文件大小（字节）

        Returns:
            下载后的文件路径
        """
        if not filename:
            filename = uuid.uuid4().hex[:12]

        safe_name = f"{filename}.{ext.lstrip('.')}"
        out_path = self.download_dir / safe_name

        try:
            async with self.session.get(url) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"下载失败 HTTP {resp.status}: {url}")

                # 流式写入
                written = 0
                with open(out_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        written += len(chunk)
                        if written > max_size:
                            f.close()
                            out_path.unlink(missing_ok=True)
                            raise RuntimeError(
                                f"文件超过大小限制 {max_size}，已取消"
                            )
                        f.write(chunk)

            logger.info(f"[抖音] 下载完成: {out_path} ({written} bytes)")
            return out_path

        except (aiohttp.ClientError, TimeoutError) as e:
            out_path.unlink(missing_ok=True)
            raise RuntimeError(f"下载网络错误: {e}") from e

    async def download_media_items(
        self, items: list[MediaItem], prefix: str = "", max_concurrent: int = 8
    ) -> list[Path]:
        """
        并发批量下载 MediaItem 列表。

        Args:
            items: 媒体列表
            prefix: 文件名前缀
            max_concurrent: 最大并发数

        Returns:
            下载后的文件路径列表（顺序与 items 一致）
        """
        sem = asyncio.Semaphore(max_concurrent)

        async def dl_one(i: int, item: MediaItem) -> Path | None:
            ext = "mp4" if item.kind in ("video", "dynamic") else "jpg"
            name = f"{prefix}{i:02d}" if prefix else f"{uuid.uuid4().hex[:10]}_{i:02d}"
            async with sem:
                try:
                    return await self.download(item.url, filename=name, ext=ext)
                except Exception as e:
                    logger.warning(f"[抖音] 下载第 {i + 1} 个媒体失败: {e}")
                    return None

        results = await asyncio.gather(*[dl_one(i, item) for i, item in enumerate(items)])
        return [r for r in results if r is not None]

    def cleanup(self, *paths: Path) -> None:
        """清理临时文件"""
        for p in paths:
            try:
                if p and p.exists():
                    p.unlink()
            except OSError:
                pass

    def cleanup_dir(self, subdir: Path | None = None) -> None:
        """清理下载目录（或指定子目录）"""
        target = subdir or self.download_dir
        if not target.exists():
            return
        for f in target.iterdir():
            try:
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    import shutil

                    shutil.rmtree(f, ignore_errors=True)
            except OSError:
                pass
