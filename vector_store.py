from datetime import datetime
from typing import Any, Dict, List, Optional
from fastembed import SparseTextEmbedding
from qdrant_client import AsyncQdrantClient, models
from sentence_transformers import SentenceTransformer

import config


class VectorStore:
    def __init__(
        self,
        host: str = config.QDRANT_HOST,
        port: int = config.QDRANT_PORT,
        collection_name: str = config.QDRANT_COLLECTION,
    ):
        self.client = AsyncQdrantClient(path="./qdrant_storage")
        self.collection_name = collection_name

        # Dense-модель (переиспользуем ту же, что и в clusterer.py)
        self.dense_model = SentenceTransformer("cointegrated/rubert-tiny2")

        # Легковесная модель для Sparse BM25 (быстро считается на CPU)
        self.sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")

    async def init_collection(self) -> None:
        """Создает коллекцию с именованными векторами и payload-индексами."""
        collections = await self.client.get_collections()
        exists = any(c.name == self.collection_name for c in collections.collections)

        if not exists:
            await self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    "dense": models.VectorParams(
                        size=config.EMBEDDING_DIM,
                        distance=models.Distance.COSINE,
                    )
                },
                sparse_vectors_config={
                    "sparse": models.SparseVectorParams(
                        index=models.SparseIndexParams(on_disk=False)
                    )
                },
            )

            # Создаем индексы для быстрых выборок
            await self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="timestamp",
                field_schema=models.PayloadSchemaType.INTEGER,
            )
            await self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="topic",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
            await self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="channel_title",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

    async def upsert_posts(self, posts: List[Dict[str, Any]]) -> None:
        """
        Индексирует список постов в Qdrant.
        Формат словаря post:
        {
            'id': int,                  # уникальный ID из SQLite
            'text': str,                # текст новости
            'channel_title': str,       # название канала
            'topic': str,            # категория (tech_ai, finance, etc.)
            'created_at': datetime/str, # дата публикации
            'message_link': str         # прямая ссылка на пост
        }
        """
        if not posts:
            return

        texts = [p["text"] for p in posts]

        # 1. Генерация Dense-эмбеддингов
        dense_embeddings = self.dense_model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        ).tolist()

        # 2. Генерация Sparse BM25-эмбеддингов
        sparse_embeddings = list(self.sparse_model.embed(texts))

        points = []
        for i, post in enumerate(posts):
            # Конвертируем дату в timestamp для удобной фильтрации диапазонов
            created_at = post.get("created_at")
            if isinstance(created_at, str):
                try:
                    dt = datetime.fromisoformat(created_at)
                    ts = int(dt.timestamp())
                except ValueError:
                    ts = int(datetime.utcnow().timestamp())
            elif isinstance(created_at, datetime):
                ts = int(created_at.timestamp())
            else:
                ts = int(datetime.utcnow().timestamp())

            point = models.PointStruct(
                id=post["id"],
                vector={
                    "dense": dense_embeddings[i],
                    "sparse": models.SparseVector(
                        indices=sparse_embeddings[i].indices.tolist(),
                        values=sparse_embeddings[i].values.tolist(),
                    ),
                },
                payload={
                    "text": post["text"],
                    "channel_title": post.get("channel_title", ""),
                    "topic": post.get("topic", "other"),
                    "timestamp": ts,
                    "date_str": str(created_at),
                    "message_link": post.get("message_link", ""),
                },
            )
            points.append(point)

        # Пакетная вставка
        await self.client.upsert(
            collection_name=self.collection_name,
            points=points,
        )

    def _build_filter(
        self,
        category: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        channel_title: Optional[str] = None,
    ) -> Optional[models.Filter]:
        """Формирует Qdrant Filter по метаданным."""
        must_conditions = []

        if category:
            must_conditions.append(
                models.FieldCondition(
                    key="category",
                    match=models.MatchValue(value=category),
                )
            )

        if channel_title:
            must_conditions.append(
                models.FieldCondition(
                    key="channel_title",
                    match=models.MatchValue(value=channel_title),
                )
            )

        if date_from or date_to:
            range_kwargs = {}
            if date_from:
                range_kwargs["gte"] = int(date_from.timestamp())
            if date_to:
                range_kwargs["lte"] = int(date_to.timestamp())

            must_conditions.append(
                models.FieldCondition(
                    key="timestamp",
                    range=models.Range(**range_kwargs),
                )
            )

        return models.Filter(must=must_conditions) if must_conditions else None

    async def search_hybrid(
        self,
        query: str,
        topic: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 7,
    ) -> List[Dict[str, Any]]:
        """
        Гибридный поиск (Dense + Sparse Reciprocal Rank Fusion) с фильтрацией.
        """
        filter_query = self._build_filter(category=topic, date_from=date_from, date_to=date_to)

        dense_vector = self.dense_model.encode(query, normalize_embeddings=True).tolist()
        sparse_vector_raw = list(self.sparse_model.embed([query]))[0]
        sparse_vector = models.SparseVector(
            indices=sparse_vector_raw.indices.tolist(),
            values=sparse_vector_raw.values.tolist(),
        )

        # Выполняем гибридный запрос (RRF-ранжирование в Qdrant)
        search_result = await self.client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                models.Prefetch(
                    query=dense_vector,
                    using="dense",
                    filter=filter_query,
                    limit=limit * 2,
                ),
                models.Prefetch(
                    query=sparse_vector,
                    using="sparse",
                    filter=filter_query,
                    limit=limit * 2,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
        )

        results = []
        for point in search_result.points:
            results.append(
                {
                    "score": point.score,
                    "text": point.payload.get("text", ""),
                    "channel_title": point.payload.get("channel_title", ""),
                    "topic": point.payload.get("topic", ""),
                    "date_str": point.payload.get("date_str", ""),
                    "message_link": point.payload.get("message_link", ""),
                }
            )
        return results

    async def close(self) -> None:
        await self.client.close()