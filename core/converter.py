"""
视频转码/拼接处理器

负责：
1. 动图片段转格式为 MP4（统一编码）
2. 多个 MP4 片段拼接
3. 合并作品音乐轨

所有操作通过调用 ffmpeg / ffprobe 命令行完成。
"""

from __future__ import annotations
from astrbot.api import logger

import asyncio
import uuid
from pathlib import Path
from typing import Sequence


# 默认输出目录
DEFAULT_OUTPUT_DIR = Path("/tmp") / "douyin_nahida" / "output"


class VideoConverter:
    """ffmpeg 转码与拼接工具"""

    def __init__(self, output_dir: str | Path | None = None, max_workers: int | None = None) -> None:
        self.output_dir = Path(output_dir or DEFAULT_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # 并发转码数：默认取 CPU 核数的一半，限幅 2~8，避免 ffmpeg 进程互相抢核
        import os
        if max_workers is None:
            max_workers = max(2, min(8, (os.cpu_count() or 4) // 2))
        self.max_workers = max_workers

    # ===== 公开方法 =====

    async def transcode_to_mp4(
        self,
        input_path: Path,
        output_path: Path | None = None,
        fps: int = 30,
        width: int | None = None,
        has_audio: bool = True,
    ) -> Path:
        """
        将任意视频/动图文件转码为统一格式的 MP4。

        - 视频流：libx264, yuv420p
        - 音频流：aac（如果源文件有音频轨）
        - 统一帧率

        Args:
            input_path: 输入文件
            output_path: 输出路径，不填则自动生成
            fps: 目标帧率
            width: 目标宽度（等比缩放），None 表示保持原宽
            has_audio: 是否保留/创建音频轨
        """
        if output_path is None:
            output_path = self.output_dir / f"{input_path.stem}_tc.mp4"

        # 检测输入是否含音频轨
        src_has_audio = await self._has_audio_stream(input_path)

        vf_filters: list[str] = []
        if width:
            vf_filters.append(f"scale={width}:-2")
        vf_filters.append(f"fps={fps}")
        vf = ",".join(vf_filters)

        cmd = [
            "ffmpeg", "-y", "-threads", "2", "-i", str(input_path),
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-an",  # 先去掉源音频，后面统一处理
            "-movflags", "+faststart",
            str(output_path),
        ]

        # 如果源文件有音频且需要保留，加音频编码
        if has_audio and src_has_audio:
            cmd = [
                "ffmpeg", "-y", "-threads", "2", "-i", str(input_path),
                "-vf", vf,
                "-c:v", "libx264",
                "-preset", "fast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(output_path),
            ]

        await self._run_ffmpeg(cmd, f"转码 {input_path.name}")
        logger.info(f"[抖音] 转码完成: {output_path}")
        return output_path

    async def concat_videos(
        self,
        inputs: Sequence[Path],
        output_path: Path | None = None,
    ) -> Path:
        """
        拼接多个已统一编码的 MP4 文件。

        使用 concat demuxer 方式（要求各片段编码一致）。
        如果只有单个片段，直接返回该片段路径。
        """
        if not inputs:
            raise ValueError("拼接列表为空")

        if len(inputs) == 1:
            return inputs[0]

        if output_path is None:
            output_path = self.output_dir / f"concat_{uuid.uuid4().hex[:8]}.mp4"

        # 创建 concat 列表文件
        list_file = self.output_dir / f"concat_list_{uuid.uuid4().hex[:8]}.txt"
        list_file.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in inputs),
            encoding="utf-8",
        )

        try:
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                "-movflags", "+faststart",
                str(output_path),
            ]
            await self._run_ffmpeg(cmd, f"拼接 {len(inputs)} 个片段")
            logger.info(f"[抖音] 拼接完成: {output_path}")
            return output_path
        finally:
            list_file.unlink(missing_ok=True)

    async def mux_audio(
        self,
        video_path: Path,
        audio_url: str | None = None,
        audio_path: Path | None = None,
        output_path: Path | None = None,
        mix: bool = False,
    ) -> Path:
        """
        给视频合并音频轨。

        - mix=False: 如果 video_path 已有音频轨，直接返回
        - mix=True: 如果 video_path 已有音频轨，用 amix 混合两层音频
        - 如果 video_path 无音频轨，直接添加新音频

        Args:
            video_path: 视频文件
            audio_url: 音频 URL（远程）
            audio_path: 音频本地路径
            output_path: 输出路径
            mix: 是否混合已有音频与新音频（动图两层音频）
        """
        has_audio = await self._has_audio_stream(video_path)
        if has_audio and not mix:
            logger.info(f"[抖音] 视频已有音频轨，跳过音频合并")
            return video_path

        if not audio_url and not audio_path:
            logger.info(f"[抖音] 无可用音频，跳过音频合并")
            return video_path

        if output_path is None:
            output_path = self.output_dir / f"{video_path.stem}_audio.mp4"

        # 确定音频源
        audio_src = str(audio_path) if audio_path else audio_url

        if has_audio and mix:
            # 混合已有音频与新音频（两层音乐）
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", audio_src,
                "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0:weights=2.2 2.2",
                "-map", "0:v:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(output_path),
            ]
            desc = "混音"
        else:
            # 视频无音频，直接添加
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", audio_src,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "128k",
                "-shortest",
                "-movflags", "+faststart",
                str(output_path),
            ]
            desc = "合并音频"

        try:
            await self._run_ffmpeg(cmd, desc)
            logger.info(f"[抖音] {desc}完成: {output_path}")
            return output_path
        except RuntimeError as e:
            logger.warning(f"[抖音] {desc}失败（可能音频不可用）: {e}")
            return video_path


    async def process_dynamic_work(
        self,
        segments: Sequence[Path],
        music_url: str = "",
        music_path: Path | None = None,
        mix_music: bool = True,
    ) -> Path:
        """
        完整处理动图作品：
        1. 每个片段转码为统一 MP4
        2. 拼接所有片段
        3. 合并作品音乐

        Returns:
            最终 MP4 路径
        """
        if not segments:
            raise ValueError("动图片段列表为空")

        logger.info(f"[抖音] 开始处理 {len(segments)} 个动图片段")

        # 1. 并发转码（统一编码参数，确保拼接顺利）
        sem = asyncio.Semaphore(self.max_workers)

        async def tc_one(idx: int, seg: Path) -> Path:
            async with sem:
                return await self.transcode_to_mp4(
                    seg,
                    has_audio=True,  # 保留片段自带音频（作品背景音乐）
                    fps=30,
                )

        transcoded: list[Path] = list(
            await asyncio.gather(*[tc_one(i, s) for i, s in enumerate(segments)])
        )

        # 2. 拼接
        merged = await self.concat_videos(transcoded)

        # 3. 合并音频
        final = await self.mux_audio(
            merged,
            audio_url=music_url or None,
            audio_path=music_path,
            mix=mix_music,  # 配置控制是否混合背景音乐
        )

        # 清理中间文件
        for tc in transcoded:
            if tc != merged and tc != final:
                tc.unlink(missing_ok=True)
        if merged != final:
            merged.unlink(missing_ok=True)

        return final

    async def process_image_slides(
        self,
        image_paths: Sequence[Path],
        music_url: str = "",
        music_path: Path | None = None,
        music_duration: float = 0.0,
        per_image_duration: float | None = None,
    ) -> Path:
        """
        将静态图片图集转为带音乐的 MP4 视频（幻灯片）。

        流程：
        1. 每张图片 → 短视频片段（libx264, 等比缩放至 1080p）
        2. 拼接所有片段
        3. 合并作品音乐

        Args:
            image_paths: 图片文件路径列表
            music_url: 音乐 URL
            music_path: 音乐本地路径
            music_duration: 音乐总时长（秒）
            per_image_duration: 每张图持续时间，不填则自动均分
        """
        if not image_paths:
            raise ValueError("图片列表为空")

        n = len(image_paths)
        if per_image_duration is None:
            if music_duration > 0:
                per_image_duration = music_duration / n
            else:
                per_image_duration = 2.0  # 默认每张 2 秒

        logger.info(
            f"[抖音] 图片转视频: {n} 张, 每张 {per_image_duration:.2f}s, "
            f"音乐 {music_duration:.1f}s"
        )

        # 1. 并发图片转视频片段
        sem = asyncio.Semaphore(self.max_workers)

        async def img_to_clip(i: int, img_path: Path) -> Path:
            clip = self.output_dir / f"clip_{uuid.uuid4().hex[:8]}.mp4"
            async with sem:
                cmd = [
                    "ffmpeg", "-y", "-threads", "2",
                    "-loop", "1",
                    "-i", str(img_path),
                    "-t", f"{per_image_duration:.3f}",
                    "-vf", "scale=1080:-2,fps=25,format=yuv420p",
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    str(clip),
                ]
                await self._run_ffmpeg(cmd, f"图片[{i+1}/{n}] 转视频")
            return clip

        clips: list[Path] = list(
            await asyncio.gather(*[img_to_clip(i, p) for i, p in enumerate(image_paths)])
        )

        # 2. 拼接
        merged = await self.concat_videos(clips)

        # 3. 合并音频
        final = await self.mux_audio(
            merged,
            audio_url=music_url or None,
            audio_path=music_path,
        )

        # 清理中间文件
        for c in clips:
            if c != merged and c != final:
                c.unlink(missing_ok=True)
        if merged != final:
            merged.unlink(missing_ok=True)

        return final

    # ===== 内部方法 =====

    async def _run_ffmpeg(self, cmd: list[str], desc: str = "") -> str:
        """执行 ffmpeg 命令，捕获输出"""
        logger.debug(f"[抖音] ffmpeg: {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"ffmpeg {desc} 失败 (code={proc.returncode}): {err}")
        return stdout.decode("utf-8", errors="replace")

    async def _has_audio_stream(self, path: Path) -> bool:
        """检测文件是否包含音频轨"""
        cmd = [
            "ffprobe", "-v", "quiet",
            "-select_streams", "a",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            str(path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return b"audio" in stdout
        except (FileNotFoundError, OSError):
            return False

    async def _get_duration(self, path: Path) -> float:
        """获取视频时长（秒）"""
        cmd = [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=duration",
            "-of", "csv=p=0",
            str(path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return float(stdout.decode("utf-8", errors="replace").strip())
        except (ValueError, FileNotFoundError, OSError):
            return 0.0

    # ===== 图文检测 =====

    async def detect_single_image(self, video_path: Path) -> bool:
        """
        检测视频是否为单张图片编译而成（抖音图文类型）。

        原理：抽取视频中两个不同时间点的帧（缩至 64x64 raw RGB），
        逐像素比较平均差值。若差值 < 5/255，判定为单张图片。

        仅依赖 ffmpeg + Python 标准库，无需 Pillow/numpy。
        """
        import tempfile, shutil

        tmpdir = Path(tempfile.mkdtemp(prefix="douyin_detect_"))
        try:
            duration = await self._get_duration(video_path)
            # 取视频 20% 和 60% 时间点的帧，避免开头/结尾的黑帧
            ts1 = max(1.0, duration * 0.2) if duration > 5 else 1.0
            ts2 = max(2.0, duration * 0.6) if duration > 5 else 3.0
            if ts1 >= ts2:
                ts1, ts2 = 1.0, 2.0

            f1 = tmpdir / "f1.raw"
            f2 = tmpdir / "f2.raw"
            await self._extract_raw_frame(video_path, ts1, f1)
            await self._extract_raw_frame(video_path, ts2, f2)

            d1 = f1.read_bytes()
            d2 = f2.read_bytes()
            n = min(len(d1), len(d2))
            if n == 0:
                return False

            mean_diff = sum(abs(a - b) for a, b in zip(d1[:n], d2[:n])) / n
            logger.info(f"[抖音] 图文检测: mean_diff={mean_diff:.1f} threshold=5.0 → {'图片' if mean_diff < 5 else '视频'}")
            return mean_diff < 5.0
        except Exception as e:
            logger.warning(f"[抖音] 图文检测失败: {e}")
            return False
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def _extract_raw_frame(self, video_path: Path, ts: float, out_path: Path, size: int = 64) -> None:
        """抽取单帧缩放为 raw RGB（用于图文检测）"""
        cmd = [
            "ffmpeg", "-y", "-ss", str(ts), "-i", str(video_path),
            "-frames:v", "1", "-vf", f"scale={size}:{size}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", str(out_path),
        ]
        await self._run_ffmpeg(cmd, "extract raw frame")
