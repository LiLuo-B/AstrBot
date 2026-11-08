"""Keep ordinary video analysis available with the configured local ASR backend."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


def test_local_asr_passes_video_backend_gate():
    path = Path(__file__).parents[1] / "data/plugins/astrbot_plugin_face_convert/main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FaceConvertPlugin")
    method = next(node for node in plugin.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "_process_video")
    method.decorator_list = []
    namespace = {"AstrMessageEvent": object, "VideoAnalysis": lambda: SimpleNamespace(notes=[], items=[]),
                 "tempfile": __import__("tempfile"), "asyncio": asyncio,
                 "logger": SimpleNamespace(info=lambda *args: None, warning=lambda *args: None)}
    # The first gate is the behavior under test; replace the expensive analysis body.
    method.body = [node for node in method.body if not isinstance(node, ast.With)]
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    plugin = SimpleNamespace(asr_key="", siliconflow_key="",
        _host_provider_usable=lambda: False,
        cfg={"local_asr_url": "http://127.0.0.1:8020/v1/audio/transcriptions"})
    result = asyncio.run(namespace["_process_video"](plugin, object(), object()))
    assert result.notes == []


def test_no_asr_backend_still_stops_before_download():
    path = Path(__file__).parents[1] / "data/plugins/astrbot_plugin_face_convert/main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    plugin_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FaceConvertPlugin")
    method = next(node for node in plugin_node.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "_process_video")
    method.decorator_list = []
    method.body = [node for node in method.body if not isinstance(node, ast.With)]
    namespace = {"AstrMessageEvent": object, "VideoAnalysis": lambda: SimpleNamespace(notes=[], items=[]),
                 "logger": SimpleNamespace(info=lambda *args: None)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    plugin = SimpleNamespace(asr_key="", siliconflow_key="",
        _host_provider_usable=lambda: False, cfg={"local_asr_url": ""})
    result = asyncio.run(namespace["_process_video"](plugin, object(), object()))
    assert result.notes == ["未配置可用的语音识别后端"]
