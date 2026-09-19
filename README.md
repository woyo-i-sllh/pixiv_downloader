# Pixiv Downloader

一个 Windows 下可直接双击运行的 Pixiv 批量图片下载器。

支持：

- 多个作品链接一次输入
- 下载作者主页的全部插画和漫画
- 从 `urls.txt` 批量读取链接
- 原图优先、并发下载和断点续传
- Clash / HTTP / HTTPS 代理
- 登录 Cookie 和 R-18 内容
- 自动使用 `作品原名-作者名-作品ID` 命名
- Pixiv 主站不可达时，对公开作品回退到图片代理

## 快速开始

1. 双击 `下载.bat`
2. 选择“下载作品链接”或“下载作者全部作品”
3. 粘贴一个或多个链接
4. 直接按回车开始下载

BAT 会在结束或报错后暂停，不会一闪而过。如果虚拟环境缺失或损坏，它会自动重建并安装依赖。

## 下载目录

默认下载目录是工具目录的上一级：

```text
F:/life/pixiv/
  るまるみるみ (115064814)/
    夢-るまるみるみ-148537766.jpg
```

规则：

- 单页：`作品原名-作者名-作品ID.ext`
- 多页：`作品原名-作者名-作品ID_p0.ext`、`_p1.ext`……
- 每件作品附带一个 `作品ID.metadata.json` 信息文件

修改 `config.json` 中的 `download_dir` 可以改变下载位置。

## 菜单功能

```text
1. 下载作品链接（支持一次输入多个）
2. 下载作者全部作品
3. 从 urls.txt 批量下载
4. 打开下载目录
5. 修改代理
6. 设置 Cookie 文件
0. 退出
```

作者菜单会询问每个作者最多下载多少件作品，直接回车表示全部。

## 代理

中国大陆网络通常需要代理才能访问 Pixiv 主站和作者作品接口。

在菜单中选择“修改代理”，例如：

```text
http://127.0.0.1:7877
```

命令行也可以通过参数临时指定：

```bat
下载.bat --proxy http://127.0.0.1:7890
```

`config.example.json` 是配置模板。首次运行环境脚本时会自动复制为本地 `config.json`。

## Cookie 与 R-18

菜单中的“设置 Cookie 文件”支持：

- Netscape 格式 `cookies.txt`
- Cookie-Editor 等扩展导出的 JSON 数组

Cookie 属于敏感信息。`config.json` 和 `cookies.txt` 已加入 `.gitignore`，不会被提交到仓库。

## 手动安装环境

双击：

```text
setup.bat
```

脚本会使用本机 Python 3.11+ 创建 `.venv` 并安装 `requirements.txt`。

也可以手动执行：

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 命令行参数

```text
--proxy URL               HTTP/HTTPS 代理
--cookies-file FILE       Cookie 文件
--limit-works N           每个作者最多下载 N 件
--prefer-original         优先原图，默认开启
--no-prefer-original      使用 regular 尺寸
--image-source auto       Pixiv 失败后回退图片代理
--image-source pixiv      只使用 Pixiv 原站
--image-source proxy      优先使用图片代理
--no-image-proxy          禁用图片代理
--overwrite               覆盖已有文件
--metadata-only           只保存信息文件
--dry-run                 只显示计划，不写文件
```

## 注意事项

- 作者全部作品需要能够访问 Pixiv 元数据接口。
- `https://pixiv.re` 是第三方公开图片回退服务，只建议作为临时方案。
- 请遵守 Pixiv 服务条款以及作品的转载、保存和使用规则。
