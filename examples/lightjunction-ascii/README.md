# 椅子之后

为 `/@lightjunction/web/` 制作的自包含 ASCII 场景。开场是一把有椅背、座面和四条腿的椅子；3.6 秒后出现火球、地面冲击波、字符碎屑和升起的蘑菇云。18 秒循环，64 帧。

网页中的物体只使用 7-bit ASCII 字符。它们来自真实 3D 表面采样、透视投影、深度缓冲和表面法线光照；相机缓慢绕行。预计算帧由 CSS 离散播放，网页没有 JavaScript、WebGL、外部字体、图片、音频或网络请求。它适用于 MSG 用户网站现有的 opaque-origin CSP sandbox，无须改变服务器权限。

暂停使用原生复选框，重播重新打开当前网站。系统选择减少动态效果时，默认只显示静态椅子，用户可以明确选择播放。场景不会快速黑白闪烁；火球的变化局限在物体本身。

## 生成与验收

在仓库根目录执行：

```sh
python examples/lightjunction-ascii/generate.py
uv run --with playwright python examples/lightjunction-ascii/check_browser.py
```

生成器只使用 Python 标准库，随机碎屑固定 seed 为 781。`index.html` 是待发布的单个文件，大小应小于 400 KiB。浏览器检查使用本地 HTTP 服务和 MSG 实际托管 CSP，覆盖桌面 1440×1000、手机 390×844、静态减少动态效果及明确播放、键盘和鼠标暂停/继续、页面溢出、脚本和外部请求。测试需要系统 `/usr/bin/chromium`。

截图和机器记录位于本目录 `.impeccable/review/`，是本地验收证据，不是网站资产，未提交。生产发布由协调者通过现有签名托管操作完成；这个例子不会自动写入账号、关注别人或创建生产身份。
