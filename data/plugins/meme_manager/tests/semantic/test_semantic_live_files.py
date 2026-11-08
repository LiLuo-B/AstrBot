import json
import threading

import pytest
from backend.semantic import index, query, storage
from backend.semantic.models import PROMPT_VERSION
from PIL import Image


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["delete", "replace", "unreadable"])
@pytest.mark.parametrize("multi_pack", [False, True])
async def test_live_file_validation_fills_top_k_without_scanning_library(
    tmp_path, monkeypatch, mutation, multi_pack
):
    class Provider:
        provider_config = {"id": "live-file-test", "embedding_model": "test-model"}
        get_dim = staticmethod(lambda: 2)

        def __init__(self):
            self.calls = []

        async def get_embedding(self, text):
            self.calls.append(text)
            if "Second" in text:
                return [0.9, 0.1]
            if "Third" in text:
                return [0.7, 0.3]
            return [1.0, 0.0]

        async def get_embeddings(self, texts):
            return [await self.get_embedding(text) for text in texts]

    pack = tmp_path / "pack"
    category = pack / "memes" / "happy"
    category.mkdir(parents=True)
    (pack / "memes_data.json").write_text(
        json.dumps({"happy": "Joy"}), encoding="utf-8"
    )
    for name, color in (("First", "red"), ("Second", "blue"), ("Third", "green")):
        source = category / f"{name}.png"
        Image.new("RGB", (8, 8), color).save(source)
        storage.save_collected_image_semantic(
            pack,
            source,
            {
                "caption": f"{name} image",
                "tags": ["joy"],
                "semantic_prompt_version": PROMPT_VERSION,
                "semantic_category": "happy",
            },
        )
    provider = Provider()
    runtime = tmp_path / "runtime"
    await index.build_index(pack, runtime, "pack", index.EmbeddingAdapter(provider))
    provider.calls.clear()
    index._QUERY_VECTOR_CACHE.clear()
    if mutation == "delete":
        (category / "First.png").unlink()
    elif mutation == "replace":
        Image.new("RGB", (8, 8), "white").save(category / "First.png")

    main_thread = threading.get_ident()
    checked_files = []
    checked_threads = []
    original_hash = index.file_sha256
    original_complete = index.semantic_caption_is_complete

    def checked_hash(source):
        checked_files.append(source.name)
        checked_threads.append(threading.get_ident())
        if mutation == "unreadable" and source.name == "First.png":
            raise PermissionError("Temporarily unreadable image")
        return original_hash(source)

    def checked_complete(item):
        checked_threads.append(threading.get_ident())
        return original_complete(item)

    monkeypatch.setattr(index, "file_sha256", checked_hash)
    monkeypatch.setattr(index, "semantic_caption_is_complete", checked_complete)
    if multi_pack:
        result = await query.search_memes_multi_pack(
            [{"pack_id": "pack", "pack_dir": pack}],
            runtime,
            "joy",
            lambda _: provider,
            top_k=1,
        )
    else:
        result = await query.search_memes(
            pack, runtime, "pack", "joy", provider, top_k=1
        )
    assert [item["caption"] for item in result["candidates"]] == ["Second image"]
    assert provider.calls == ["joy"]
    assert "Second.png" in checked_files
    assert "Third.png" not in checked_files
    assert len(checked_files) == len(set(checked_files))
    assert checked_threads and main_thread not in checked_threads
