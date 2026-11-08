# face_convert：看图/听话不再依赖 DashScope（2026-11-07）

## 背景

`df_bili_watch` 抽帧后要交给 `face_convert._vl_describe()` 做视觉理解，
无字幕视频还要 `_asr_file()` 做转写。这两条路原先只认 DashScope：

```python
async def _mm_call(self, model, content_items, timeout=60):
    if not self.asr_key:
        return None            # 静默返回，不报错
    ...  # 写死 https://dashscope.aliyuncs.com/api/v1
```

`asr_api_key` 为空时**静默降级**——帧抽出来了但没人看，UI 上看不到任何失败。

这套环境里 `asr_api_key` / `DASHSCOPE_API_KEY` / dashscope provider 三者全空，
所以视觉和转写实际都不可用。

## 关键认识

**系统本身具备视觉能力，只是插件没用宿主的 provider 体系。**
实测现有 newapi provider：

| 模型 | 看图 |
|---|---|
| `deepseek-v4.1-flash` | ✅ 准确识别出 Rickroll 画面 |
| `minimax-m3.1-flash` | ✅ 正确描述人物 |
| `gemini-3.8-flash` | ❌ 中转里没这个模型（`model_not_found`）|

转写方面，**现有硅基流动 key 直接支持** `/v1/audio/transcriptions`：

```
POST https://api.siliconflow.cn/v1/audio/transcriptions
model=Qwen/Qwen3-ASR-1.7B
→ {"text": "今天天气不错，我们一起出去走走吧，顺便买点鱼食回来。", "language": "Chinese"}
```

## 硅基流动 ASR 模型选型

该平台共 6 个可用音频模型，实测对比（同一段中文语音，
原文「今天天气不错，我们一起出去走走吧。顺便买点鱼食回来。」）：

| 模型 | 转写结果 | 附加 | 耗时 |
|---|---|---|---|
| `Qwen/Qwen3-ASR-1.7B` | 完全正确 | 无 | **~0.9–1.1s** |
| `FunAudioLLM/SenseVoiceSmall` | 完全正确 | **多出 😊** | ~4.0s |
| `XingChenAGI/XingChenASR-V3.2` | 「一起」→「一七」 | 无 | — |
| `XingChenAGI/XingChenASR-V3.2-Ultra` | 漏「一起出去走走吧」 | 无 | — |
| `XingChenAGI/XingChenASR-Diarize-V3.0` | 同样漏词，但带说话人分段 | 有 segments | — |
| `FunAudioLLM/CosyVoice2-0.5B` | TTS 模型，非 ASR | — | — |

**选 `Qwen/Qwen3-ASR-1.7B`**，理由：

1. **不吐 emoji**——SenseVoiceSmall 会固定追加情绪 emoji（三次复现一致），
   混进 `df_activity_note` 与记忆队列会污染文本。
2. **快 4 倍**——24.8 秒音频 0.9s vs 4.0s，视频切片转写可叠加。
3. 中文准确率最高，标点也比 SenseVoice 更规范（用「。」而非「，」结尾）。
4. 稳定性：连测三次均 0.89/1.00/1.12 秒，输出长度一致。

英文同样准确（`Hello, world. This is a test.` vs 原文
`hello world this is a test`）。

## 改动

新增**三后端可选**，DashScope 降级为其中之一（向后兼容）：

| 后端 | 触发条件 | 用途 |
|---|---|---|
| `dashscope` | `asr_api_key` 非空 | 原行为，老部署不受影响 |
| `host` | 宿主 provider 可用 | 复用 AstrBot 的 `text_chat(image_urls/audio_urls)` |
| `siliconflow` | 有硅基流动 key | `/v1/audio/transcriptions`，仅转写 |

视觉顺序：DashScope（若配了）→ 宿主 provider。
转写顺序由 `asr_backend` 决定，`auto` 时按 DashScope → 宿主 → 硅基流动。

新增配置项（`config.json`，全部有默认值）：

```json
"vl_provider_id": "",          // 留空=自动挑支持 image 的
"stt_provider_id": "",         // 留空=读 provider_stt_settings，其次按音频模态
"asr_backend": "auto",         // auto|dashscope|host|siliconflow
"siliconflow_api_key": "",     // 留空=复用 embedding/rerank 的凭证
"siliconflow_asr_model": "Qwen/Qwen3-ASR-1.7B",
"siliconflow_asr_base": "https://api.siliconflow.cn/v1"
```

`siliconflow_api_key` 留空时会从 `cmd_config.json` 的 embedding/rerank provider
自动取（要求 `api_base` 含 `siliconflow`），所以本环境无需手填。

## 新增方法

| 方法 | 作用 |
|---|---|
| `_backend_order()` | 解析转写后端优先级 |
| `_resolve_host_provider(want_image)` | 按配置/模态挑宿主 provider |
| `_describe_via_host()` | 视觉走 `text_chat(image_urls=...)` |
| `_transcribe_via_host()` | 转写走 `text_chat(audio_urls=...)` |
| `_transcribe_via_siliconflow()` | 硅基流动 SenseVoice |
| `_host_provider_usable()` | 宿主 provider 是否可用 |

## 验证（真实调用）

**视觉**（三帧真实视频画面，走宿主 `deepseek-v4.1-flash`）：

```
- 00:00：画面全黑，无可见人物、动作、场景或文字。
- 00:05：红发男子穿深色西装、条纹内搭，立于复古麦克风前，背景为镂空花格窗。
- 00:11：红发男子戴墨镜、穿浅蓝衬衫，侧身站在铁丝网前，面带微笑。
```

**转写**（`flite` 生成的 "hello world this is a test"，走硅基流动）：

```
Hello world, this is a test.
```

**后端识别**日志：

```
[face_convert] 表情字典载入完成，共 326 条；语音转写已就绪
（未配置；可用后端=宿主provider/硅基流动；模型 qwen3-asr-flash/qwen3-vl-plus）
```

插件重载后正常工作（日志有真实消息处理记录），`activated: True`。

## 未改动

- `asr_api_key` 保持空（不启用 DashScope）
- 表情转换主功能（`convert_faces`）未触碰
- AstrBot 的 `provider_stt_settings` 未改（仍是 `enable: false`）

## 回滚

```
cp /tmp/face_main.py.bak-20261107-231622 data/plugins/astrbot_plugin_face_convert/main.py
cp /tmp/face_config.json.bak-20261107-231622 data/plugins/astrbot_plugin_face_convert/config.json
```
然后在面板重载 `astrbot_plugin_face_convert`。

## 注意

`minimax-m3.1-flash` 虽然配置里 `modalities` 含 `audio`，但实测
`input_audio` 内容块被上游拒绝（`11101 unsupported content type`），
所以**不能**用它做原生音频转写。转写走独立的 `/audio/transcriptions` 端点。
