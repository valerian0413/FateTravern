"""Hybrid retrieval over committed player-visible data. SQLite owns the source records."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from threading import Lock

from app.application.context import build_intent_context
from app.storage.repositories import WorldRepo

logger = logging.getLogger(__name__)
LIMIT = 8


async def search_memory(runtime, world_id: str, query: str) -> dict:
    snapshot = await WorldRepo(runtime.db.conn).get_snapshot(world_id)
    public = (await build_intent_context(snapshot, query))['public']
    history = await runtime.db.fetchall(
        "SELECT id, turn_id, author, text FROM messages WHERE world_id=? AND channel='narrative' ORDER BY sequence DESC",
        (world_id,),
    )
    rows = [
        *(('entity', item) for item in public['entities']),
        *(('fact', item) for item in public['visible_facts']),
        *(('world_book', entry) for entries in (public['world_book'] or {}).get('sections', {}).values()
          for entry in entries),
        *(('narrative', dict(message)) for message in history),
    ]
    # Content-addressed IDs prevent stale versions from being returned after a fact changes.
    documents = {}
    for source, row in rows:
        record = {'source': source, **row}
        content = json.dumps(record, ensure_ascii=False, sort_keys=True)
        index_id = hashlib.sha256(content.encode()).hexdigest()
        values = [row[key] for key in ('name', 'aliases', 'predicate', 'value', 'text') if key in row]
        searchable = ' '.join(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                              for value in values)
        documents[index_id] = (record, searchable)
    query = query.strip().casefold()[:300]
    terms = query.split()

    def keyword_rank(content):
        content = content.casefold()
        return 2 if query and query in content else 1 if terms and all(term in content for term in terms) else 0

    keywords = sorted(documents, key=lambda key: -keyword_rank(documents[key][1]))
    keywords = [key for key in keywords if keyword_rank(documents[key][1])][:LIMIT]
    vectors = []
    mode = 'keyword'
    if runtime.memory_index and documents:
        try:
            vectors = await runtime.memory_index.query(world_id, snapshot.world.player_id, documents, query)
            mode = 'hybrid'
        except Exception:
            logger.exception('世界 %s 的向量索引暂不可用，使用关键词检索', world_id)
            mode = 'keyword_fallback'

    # Reserve space for semantic matches, keeping exact names first.
    ordered = list(dict.fromkeys([*keywords[:4], *vectors, *keywords]))[:LIMIT]
    matches = []
    for key in ordered:
        record, content = documents[key]
        matches.append(record if len(json.dumps(record, ensure_ascii=False)) <= 1200 else {
            'source': record['source'], 'id': record['id'], 'excerpt': content[:1200],
        })
    return {'mode': mode, 'matches': matches}


class MemoryIndex:
    """Local Chroma cache; use an ONNX encoder to keep texts on this machine."""

    def __init__(self, path, model_path):
        self.path = path
        self.model_path = model_path
        self.client = None
        self.encoder = None
        self.lock = Lock()

    def _encode(self, texts):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        if self.encoder is None:
            tokenizer = Tokenizer.from_file(str(self.model_path / 'tokenizer.json'))
            tokenizer.enable_padding()
            tokenizer.enable_truncation(max_length=512)
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            session = ort.InferenceSession(str(self.model_path / 'model.onnx'), sess_options=options,
                                           providers=['CPUExecutionProvider'])
            self.encoder = tokenizer, session
        tokenizer, session = self.encoder
        encoded = tokenizer.encode_batch(texts)
        values = {'input_ids': [item.ids for item in encoded],
                  'attention_mask': [item.attention_mask for item in encoded],
                  'token_type_ids': [item.type_ids for item in encoded]}
        inputs = {name.name: np.asarray(values[name.name], dtype=np.int64) for name in session.get_inputs()}
        output = session.run(None, inputs)[0]
        return output[:, 0, :] if output.ndim == 3 else output

    async def query(self, world, observer, documents, text):
        return await asyncio.to_thread(self._query, world, observer, documents, text)

    def _query(self, world, observer, documents, text):
        with self.lock:
            return self._query_locked(world, observer, documents, text)

    @staticmethod
    def _collection_name(world, observer):
        namespace = hashlib.sha256(f'{world}:{observer}'.encode()).hexdigest()
        return 'bge-zh-v1-' + namespace[:40]

    def _client(self):
        if self.client is None:
            import chromadb

            self.client = chromadb.PersistentClient(path=str(self.path))
        return self.client

    def delete_world(self, world, observer):
        with self.lock:
            if self.client is None and not self.path.is_dir():
                return
            from chromadb.errors import NotFoundError

            try:
                self._client().delete_collection(self._collection_name(world, observer))
            except NotFoundError:
                pass

    def _query_locked(self, world, observer, documents, text):
        collection = self._client().get_or_create_collection(
            name=self._collection_name(world, observer), embedding_function=None,
            configuration={'hnsw': {'space': 'cosine'}},
        )
        indexed = set(collection.get(include=[])['ids'])
        fresh = [key for key in documents if key not in indexed]
        for offset in range(0, len(fresh), 64):
            batch = fresh[offset:offset + 64]
            embeddings = self._encode([documents[key][1][:1600] for key in batch])
            collection.upsert(ids=batch, embeddings=embeddings.tolist())
        obsolete = list(indexed - documents.keys())
        for offset in range(0, len(obsolete), 256):
            collection.delete(ids=obsolete[offset:offset + 256])
        result = collection.query(
            query_embeddings=self._encode(['为这个句子生成表示以用于检索相关文章：' + text]).tolist(),
            n_results=min(len(documents), LIMIT), include=['distances'],
        )
        return [key for key in result['ids'][0] if key in documents]
