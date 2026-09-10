# clusterer.py
from datetime import datetime
from typing import List, Dict, Any
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.cluster import AgglomerativeClustering
from sklearn.metrics.pairwise import cosine_distances


class EventClusterer:
    def __init__(
        self,
        model_name: str = "cointegrated/rubert-tiny2",
        distance_threshold: float = 0.35,  # для rubert-tiny2 рабочий порог ~0.30 - 0.40
        time_window_hours: float = 18.0
    ):
        """
        :param distance_threshold: Порог косинусного расстояния (0.18 - 0.25). 
                                   Чем меньше, тем строже схожесть.
        :param time_window_hours: Посты с разницей во времени больше этого порога 
                                  не объединяются в одно событие.
        """
        # Модель автоматически использует CUDA, если доступна RTX 4080
        self.model = SentenceTransformer(model_name)
        self.distance_threshold = distance_threshold
        self.time_window_hours = time_window_hours

    def _parse_date(self, date_val: Any) -> datetime:
        if isinstance(date_val, datetime):
            return date_val
        try:
            return datetime.fromisoformat(str(date_val))
        except Exception:
            return datetime.now()

    def cluster_messages(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        if not messages:
            return []
        if len(messages) == 1:
            return [[messages[0]]]

        # 1. Формируем тексты с префиксом 'passage: ' для e5-моделей
        texts = [f"passage: {m['text'][:1000].strip()}" for m in messages]

        # 2. Вычисляем нормализованные эмбеддинги
        embeddings = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

        # 3. Матрица косинусных расстояний (0 = идентичны, 1 = ортогональны)
        dist_matrix = cosine_distances(embeddings)

        # 4. Временной штраф: если разница во времени больше окна, ставим максимальное расстояние
        dates = [
            self._parse_date(m.get("date") or m.get("created_at") or m.get("timestamp")) 
            for m in messages
        ]
        n = len(messages)
        for i in range(n):
            for j in range(i + 1, n):
                diff_hours = abs((dates[i] - dates[j]).total_seconds()) / 3600.0
                if diff_hours > self.time_window_hours:
                    dist_matrix[i, j] = 1.0
                    dist_matrix[j, i] = 1.0

        # 5. Иерархическая кластеризация по предварительно вычисленным расстояниям
        clustering = AgglomerativeClustering(
            n_clusters=None,
            metric="precomputed",
            linkage="average",
            distance_threshold=self.distance_threshold
        )
        labels = clustering.fit_predict(dist_matrix)

        # 6. Группируем посты по меткам кластеров
        clusters: Dict[int, List[Dict[str, Any]]] = {}
        for idx, label in enumerate(labels):
            clusters.setdefault(label, []).append(messages[idx])

        return list(clusters.values())