# 纳西妲抖音下载

> AstrBot 插件 · 自动识别抖音链接，去水印下载视频、实况照片动图和图集，动图自动拼接为 MP4 并保留作品音乐。

## ✨ 功能

| 作品类型 | 处理方式 | 输出 |
|---------|---------|------|
| 普通视频 | 直接下载无水印 MP4 | MP4 视频 |
| 图文类型（单图+音乐） | 下载视频 → 帧对比检测 → 提取封面图 | 图片消息 |
| 实况照片 / Live Photo 动图 | 并发下载所有片段 → 转码 → 拼接 → 合并音乐 | MP4 视频 |
| 纯图片图集（含图集+音乐） | 直接下载所有图片 | 图片消息 |

### 核心特性

- 🔗 **自动触发**：发送包含抖音链接的消息即可，无需额外命令
- 🚫 **无水印**：通过网页详情 API 提取无水印原始视频流
- 🔍 **图文检测**：下载后自动抽帧对比像素差值，识别抖音图文类型（单图+音乐编译的视频），提取封面图直接发送，不发视频
- ⚡ **并发加速**：下载和转码均使用并发（`asyncio.Semaphore`），适配多核 CPU
  - 下载：默认 8 路并发
  - 转码：按 CPU 核数自适应（`min(8, cpu_count // 2)`），每进程限 2 线程
- 🎵 **保留音乐**：动图作品自动提取配乐 URL，合并到最终视频
- 📸 **图集直发**：所有纯图片图集（含带音乐的）直接发送图片，不转视频
- 🔧 **签名自动重试**：抖音 ABogus 签名 API 间歇 403，内置自动重试
- 🔍 **多路径解析**：网页详情 API → slidesinfo API → 分享页 `_ROUTER_DATA`，三路 fallback 确保成功率
- 🎬 **ffmpeg 全流程**：转码（libx264）→ concat 拼接 → 音频合并（aac）

## 📦 依赖

| 依赖 | 用途 |
|------|------|
| [f2](https://github.com/Evil0ctal/F2) | 生成抖音 ABogus 签名（`ABogusManager`） |
| aiohttp | 异步 HTTP 下载 |
| pyyaml | 配置解析 |
| ffmpeg / ffprobe | 视频转码、拼接、音频合并（系统级安装） |

## 🚀 安装

1. 将插件放入 `data/plugins/astrbot_plugin_douyin_nahida/` 目录
2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
3. 确保系统已安装 ffmpeg：
   ```bash
   ffmpeg -version
   ```
4. 在 AstrBot 管理面板重载插件

## ⚙️ 配置

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `cookie` | 抖音网页版 Cookie（含 `ttwid` 等），可提升解析成功率。留空则自动注册匿名 `ttwid` | 空 |

获取 Cookie 方法：浏览器打开 [douyin.com](https://www.douyin.com) → F12 → Network → 任意请求 → 复制 Cookie 头。

## 📖 使用

直接在聊天中发送抖音链接即可：

```
https://v.douyin.com/iuYezDXSGd0/
```

插件会自动识别并处理，完成后发送视频或图片到聊天。

### 支持的链接格式

- 短链：`https://v.douyin.com/xxxxx`
- 分享口令中的链接（自动提取）
- 完整链接：`https://www.douyin.com/video/xxx`
- 笔记链接：`https://www.douyin.com/note/xxx`
- 图集链接：`https://www.douyin.com/slides/xxx`

## 🏗️ 架构

```
astrbot_plugin_douyin_nahida/
├── main.py                  # 插件入口，正则匹配 + 路由分发
├── core/
│   ├── __init__.py           # 模块导出
│   ├── parser.py             # 链接解析器（短链重定向 + f2 签名 + 三路 API fallback）
│   ├── downloader.py         # 并发下载器（asyncio.Semaphore）
│   └── converter.py          # 转码/拼接/音频合并（ffmpeg）
├── metadata.yaml             # 插件元数据
├── requirements.txt          # Python 依赖
├── _conf_schema.json         # 配置项定义
└── README.md
```

### 处理流程

```
用户发送抖音链接
        │
        ▼
  ① 解析（parser.py）
     ├─ 短链 301 重定向 → 获取 aweme_id
     ├─ 路径 A：f2 ABogusManager 签名 → 网页详情 API（含 Live Photo video 字段）
     ├─ 路径 B：slidesinfo API（动图/图集）
     └─ 路径 C：分享页 _ROUTER_DATA（兜底）
        │
        ▼
  ② 下载（downloader.py）
     └─ 并发下载所有媒体片段（Semaphore=8）
        │
        ▼
  ③ 处理（converter.py）
     ├─ 动图：并发转码 → concat 拼接 → 合并音乐 → MP4
     ├─ 图文检测：抽两帧 64x64 raw RGB → 逐像素对比 → 差值<5 则判定为图片 → 发封面
     └─ 纯图片：跳过，直接发送
        │
        ▼
  ④ 发送结果到聊天
```

### 模块说明

#### `parser.py` — 链接解析器

- `DouyinParser` 类，异步解析抖音链接
- `find_douyin_url(text)` 静态方法，从消息文本提取链接
- `parse(url)` 主入口，返回 `DouyinWorkInfo`
- 三路 fallback 解析策略：
  - `_parse_web_detail()`：f2 签名 + 网页详情 API，可获取 Live Photo 的 `image[].video` 字段
  - `_parse_slides_api()`：slidesinfo API，动图/图集专用
  - `_parse_share_page()`：分享页 `_ROUTER_DATA` 解析，兜底方案
- `probe_video_url(video_id)`：探测无水印视频直链（iesdouyin 分享页方式）
- `_resolve_short_link()`：短链 301 重定向
- `_ensure_ttwid()`：自动注册匿名 ttwid cookie
- `_extract_music()`：提取作品配乐信息（URL、标题、时长）

#### `downloader.py` — 并发下载器

- `DouyinDownloader` 类，异步下载媒体文件
- `download(url, filename, ext)`：下载单个文件
- `download_media_items(items, prefix, max_concurrent)`：并发批量下载
  - 默认 8 路并发（`asyncio.Semaphore`）
  - 自动识别文件类型（mp4 / jpg）
  - 失败自动跳过并告警

#### `converter.py` — 转码/拼接/音频合并

- `VideoConverter` 类，基于 ffmpeg 的视频处理工具
- `transcode_to_mp4(input, output, fps, width, has_audio)`：转码为统一 MP4（libx264, yuv420p）
- `concat_videos(inputs, output)`：concat demuxer 拼接多个 MP4
- `mux_audio(video, audio_url)`：合并音乐轨（aac, 128k）
- `process_dynamic_work(segments, music_url)`：动图全流程（转码→拼接→合并音乐）
- `process_image_slides(image_paths, music_url, music_duration)`：图片转视频全流程
- `detect_single_image(video_path)`：图文检测（抽两帧 64x64 raw RGB 逐像素对比，差值<5 判定为单图）
- 并发转码：`asyncio.Semaphore(max_workers)`，按 CPU 自适应
- ffmpeg 每进程限 `-threads 2`，避免多核争抢

## 📊 性能基准

| 作品类型 | 片段数 | 并发耗时 |
|---------|--------|---------|
| 多段 Live Photo | 22 段 | ~9s |
| 单段 Live Photo | 1 段 | ~1.9s |
| 纯图片图集 | 1 张 | ~0.9s |
| 普通视频 | 1 个 | ~4.1s |

> 测试环境：16 核 CPU，解析受抖音签名 API 间歇 403 影响，实际耗时可能波动。

## 📝 更新日志

### v1.2.2
- 动图输出新增配置开关 `dynamic_mix_music`：控制是否混合作品背景音乐（默认开启）
  - 开启：片段原声 + 背景音乐两层混音
  - 关闭：只保留动图片段本身的声音
  - 配置路径：插件管理 → 纳西妲抖音下载 → 动图是否混合作品背景音乐
- `process_dynamic_work` 新增 `mix_music` 参数，由配置传入

### v1.2.1
- 修复动图（Live Photo）背景音乐丢失问题：转码时保留片段自带音频轨，不再强制 `-an` 去除
- `mux_audio` 新增 `mix` 参数：动图作品使用 `mix=True`，通过 ffmpeg `amix` 滤镜混合两层音频（片段原声 + 作品背景音乐）
- `mux_audio` 检测到已有音频轨且 `mix=False` 时自动跳过（图集等场景不受影响）

### v1.2.0
- 新增图文类型检测：下载视频后抽两帧（64x64 raw RGB）逐像素对比，差值<5 判定为单张图片
- 图文类型自动提取封面图发送，不发编译视频
- `DouyinWorkInfo` 新增 `cover_url` 字段
- `VideoConverter` 新增 `detect_single_image()` 和 `_extract_raw_frame()` 方法
- 仅依赖 ffmpeg + Python 标准库，无需 Pillow/numpy

### v1.1.1
- 删除 `needs_image_to_video` 分支，静态图片+音乐图集不再转视频，直接发图
- 路由简化为 3 条：动图转视频 / 普通视频下载 / 图片直接发送
- 下载改为并发（`Semaphore=8`），速度提升 ~2.8x
- 转码改为并发（按 CPU 自适应），速度提升 ~2.1x
- ffmpeg 每进程限 2 线程，避免多核争抢
- logger 统一使用 `astrbot.api.logger`，修复 `plugin_tag` 报错
- 插件名改为 `astrbot_plugin_douyin_nahida`，显示名「纳西妲抖音下载」

### v1.0.0
- 初始版本
- 支持普通视频、实况照片动图、纯图片图集
- f2 签名 + 网页详情 API + slidesinfo API + 分享页三路 fallback
- 动图片段拼接 + 音乐合并

## 📄 License

MIT

## 👤 作者

**纳西妲** — 须弥智慧之神 🌿

> 知识，就像一粒种子，需要耐心灌溉。～✿
