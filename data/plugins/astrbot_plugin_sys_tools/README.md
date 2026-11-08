# 系统工具集

`query_public_ip` 查询宿主机出口 IP；`query_server_status` 使用迁移自 NoneBot PicStatus 的模板直接发图。仍由 AI 选择工具，不增加第二套命令回复，也不改变模型路由或人格白名单。

## 配置入口

AstrBot WebUI → 插件 → 系统工具集 → 配置 → `status`。保存后热加载，不必重启聊天平台。下表列出与上游的对应关系；AstrBot 中使用右侧名称，不读取 NoneBot 的 `.env`。

| 上游 | 本插件 `status` 字段 | 行为 |
|---|---|---|
| PS_DEFAULT_COMPONENTS | components | 显示区块及顺序，删除即隐藏：header/cpu_mem/gpu/disk/network/process/footer |
| PS_DEFAULT_PIC_FORMAT | image_format | png/jpeg；保留现有 PNG 默认 |
| PS_DEFAULT_ADDITIONAL_CSS | additional_css | 内置 no-shadow/no-radius/no-blur；不执行任意脚本 |
| PS_SHOW_CURRENT_BOT_ONLY | show_current_bot_only | 当前平台或所有装配的聊天平台 |
| PS_USE_ENV_NICK / NICKNAME | use_env_nick / nickname | 使用自定义昵称替代平台昵称 |
| PS_IGNORE_PARTS | ignore_parts | 正则匹配挂载点路径 |
| PS_IGNORE_BAD_PARTS | ignore_bad_parts | 隐藏读取失败的挂载点 |
| PS_SORT_PARTS / PS_SORT_PARTS_REVERSE | sort_parts / sort_parts_reverse | 按空间使用百分比降序 / 反转为升序 |
| PS_IGNORE_DISK_IOS | ignore_disk_ios | 正则匹配 IO 设备名 |
| PS_IGNORE_NO_IO_DISK / PS_SORT_DISK_IOS | ignore_no_io_disk / sort_disk_ios | 隐藏零读写 / 按合计读写速率排序 |
| PS_IGNORE_NETS | ignore_nets | 正则匹配网卡名称；默认额外隐藏 veth/br-/tun/tap，可删除规则 |
| PS_IGNORE_0B_NET / PS_SORT_NETS | ignore_0b_net / sort_nets | 隐藏零收发 / 按合计收发速率排序 |
| PS_PROC_LEN / PS_PROC_SORT_BY | top_processes / process_sort_by | 0–100 条，按 cpu 或 mem；保留当前内存 TOP10 |
| PS_IGNORE_PROCS | ignore_procs | 先过滤进程名，再取排行 |
| PS_PROC_CPU_MAX_100P | proc_cpu_max_100p | 将 CPU% 除以逻辑核心数，不是简单截断为 100 |
| proxy / PS_TEST_SITES | proxy / test_sites | 一个测试代理 + 每站 name/url/use_proxy；表单添加/删除决定数量 |
| PS_TEST_TIMEOUT / PS_SORT_SITES | test_timeout / sort_sites | 每站总超时；排序开启时按上游失败在前、成功延迟递增 |
| PS_BG_PROVIDER / PS_BG_LOCAL_PATH / PS_BG_URL | background_source / background_path / background_url | loli 每次随机、local 文件或目录随机、url 自定义图源 |
| PS_DEFAULT_AVATAR | default_avatar | 无平台头像时使用的默认头像 URL |
| PS_ONLY_SU | only_admin | 只允许管理员调用状态工具 |

另外可配置 `theme=dark/light`、`show_gpu`、`network_interfaces`（网卡白名单）、`disk_devices`（IO 设备白名单）、`background_proxy` 和 `background_timeout`。空白名单表示自动发现，不再有隐含的前三网卡截断；忽略正则和排序仍生效。正则无效会明确报错，不悄悄过滤全部数据。

`test_sites` 的条目由 WebUI 模板表单编辑，JSON 形式示例：

```json
{
  "__template_key": "site",
  "name": "Google",
  "url": "https://www.google.com/",
  "use_proxy": true
}
```

`url` 支持带协议的域名或 IP、端口；这是 HTTP 耗时，不是 ICMP Ping。列表为空关闭探测。`proxy=http://127.0.0.1:7890` 仅供 `use_proxy=true` 的条目使用，百度可直连，不影响公网 IP 查询或模型。`background_proxy` 单独控制背景下载，空值为直连。

## 随机背景修复

旧实现把随机背景成功和失败兜底都缓存一小时，造成图片固定。现在远程背景每次发图重新请求，附带随机参数防止图源/CDN缓存同一响应；只把上次成功图片留作失败兜底，下次查询仍重试。头像继续缓存一小时。随机图源本身仍可能返回重复图片，不能保证每次必然不同。

`background_source=local` 时，路径可为一张固定图或图片目录；目录内随机，存在多张有效图时避免连续重复。其他模式不会被非空本地路径意外覆盖。背景下载限 10 MiB，并有总超时；`background_retry_limit` 默认 3 次处理随机图片链接 404，所有尝试共享 `background_timeout`，不是每次重新等待。图片按实际字节验证、编码，浏览器不请求外网。

## 迁移边界（没有假装全部上游功能都已实现）

- NoneBot 的命令别名、need_at、回复引用由 AstrBot 自身的唤醒、会话与 AI 工具处理，不重复注册命令。
- 周期采集和 deque 历史缓存、图源预加载任务队列、Lolicon/其他图源插件未迁移；当前按请求实时采样，支持 loli/local/url，未声明无效配置开关。
- OneBot 的 get_status 账号级统计、断线清零事件与 message_sent 全局监听未移植；当前计数明确标注“AI 请求/回复 · 插件加载以来”，按平台统计，包括该平台群聊触发 AI 的请求，不等同账号全部消息。
- 单独设置 `show_current_bot_only=false` 可以显示所有平台；默认仍保持当前平台，WebChat 预览优先显示实际 QQ 官方机器人。
- 任意额外 JavaScript、第三方模板自动加载未开放；只允许随插件打包的样式，避免外网脚本和资源影响渲染。

上游参考：https://github.com/lgc-NB2Dev/nonebot-plugin-picstatus

## 测试与部署

依赖在 requirements.txt；Pillow 已随现有 AstrBot 环境安装，此次只声明已有依赖。首次部署需给 Playwright 安装 Chromium：

```bash
python -m playwright install chromium
.venv/bin/python -m pytest -q --noconftest data/plugins/astrbot_plugin_sys_tools/tests
```

`tests/preview_live.py` 为手动线上发图验证，会创建测试 WebChat 会话，不由 pytest 自动执行，不修改全局模型设置。
