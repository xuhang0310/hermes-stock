"""Rebuild all production vector collections in Milvus from MySQL source rows.

The command never reads Chroma as a source and does not mutate MySQL. It creates
or replaces knowledge chunks, active user memories, and non-archived experience
entries, then verifies row counts and performs an exact-text recall smoke test.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select

from backend.app.ai import memory
from backend.app.core.database import SessionLocal
from backend.app.core.models import Document, DocumentChunk, DocumentSymbol, ExperienceEntry, UserMemory
from backend.app.core.settings import settings


@dataclass(frozen=True)
class VectorRecord:
    key: str | int
    text: str
    metadata: dict[str, Any]


def _load_sources() -> tuple[dict[str, list[VectorRecord]], list[str]]:
    symbol = (
        select(DocumentSymbol.symbol)
        .where(DocumentSymbol.doc_id == DocumentChunk.doc_id)
        .limit(1)
        .scalar_subquery()
    )
    with SessionLocal() as db:
        knowledge_rows = db.execute(
            select(
                DocumentChunk.vector_id,
                DocumentChunk.content,
                DocumentChunk.doc_id,
                DocumentChunk.embed_model,
                Document.doc_type,
                Document.trust_level,
                Document.published_at,
                symbol.label("symbol"),
            )
            .join(Document, Document.id == DocumentChunk.doc_id)
            .where(DocumentChunk.status == "indexed")
            .order_by(DocumentChunk.id)
        ).all()
        memory_rows = db.scalars(
            select(UserMemory)
            .where(UserMemory.status == "active")
            .order_by(UserMemory.id)
        ).all()
        experience_rows = db.scalars(
            select(ExperienceEntry)
            .where(ExperienceEntry.status != "archived")
            .order_by(ExperienceEntry.id)
        ).all()

        knowledge = [
            VectorRecord(
                key=row.vector_id,
                text=row.content,
                metadata={
                    "doc_id": row.doc_id,
                    "doc_type": row.doc_type or "",
                    "symbol": row.symbol or "",
                    "trust_level": row.trust_level or "",
                    "published_at": int(row.published_at.timestamp()) if row.published_at else 0,
                },
            )
            for row in knowledge_rows
            if row.vector_id and (row.content or "").strip()
        ]
        memories = [
            VectorRecord(
                key=row.id,
                text=row.content,
                metadata={
                    "user_id": row.user_id,
                    "memory_type": row.memory_type or "",
                    "importance": float(row.importance or 0.0),
                },
            )
            for row in memory_rows
            if (row.content or "").strip()
        ]
        experiences = [
            VectorRecord(
                key=row.id,
                text=row.content,
                metadata={
                    "status": row.status,
                    "tags": row.tags_json or "",
                    "market_env": row.market_env_json or "",
                },
            )
            for row in experience_rows
            if (row.content or "").strip()
        ]
        source_models = sorted({row.embed_model for row in knowledge_rows if row.embed_model})

    return {
        "knowledge": knowledge,
        "memory": memories,
        "experience": experiences,
    }, source_models


class MilvusMigration:
    def __init__(self, uri: str, token: str, reset: bool, batch_size: int) -> None:
        from pymilvus import DataType, MilvusClient

        self.client = MilvusClient(uri=uri, token=token or "")
        self.DataType = DataType
        self.reset = reset
        self.batch_size = batch_size

    def close(self) -> None:
        self.client.close()

    def _ensure_collection(self, kind: str, name: str, dim: int) -> None:
        if self.client.has_collection(name):
            if not self.reset:
                self.client.load_collection(name)
                return
            self.client.drop_collection(name)

        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=kind != "experience")
        if kind == "knowledge":
            schema.add_field("vector_id", self.DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("vector", self.DataType.FLOAT_VECTOR, dim=dim)
            schema.add_field("doc_id", self.DataType.INT64)
            schema.add_field("doc_type", self.DataType.VARCHAR, max_length=32)
            schema.add_field("symbol", self.DataType.VARCHAR, max_length=24)
            schema.add_field("trust_level", self.DataType.VARCHAR, max_length=24)
            schema.add_field("published_at", self.DataType.INT64)
        elif kind == "memory":
            schema.add_field("id", self.DataType.INT64, is_primary=True)
            schema.add_field("vector", self.DataType.FLOAT_VECTOR, dim=dim)
            schema.add_field("user_id", self.DataType.INT64)
        else:
            schema.add_field("id", self.DataType.INT64, is_primary=True)
            schema.add_field("vector", self.DataType.FLOAT_VECTOR, dim=dim)
            schema.add_field("status", self.DataType.VARCHAR, max_length=24)
            schema.add_field("tags", self.DataType.VARCHAR, max_length=4096)
            schema.add_field("market_env", self.DataType.VARCHAR, max_length=4096)

        indexes = self.client.prepare_index_params()
        indexes.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
        self.client.create_collection(
            collection_name=name,
            schema=schema,
            index_params=indexes,
            consistency_level="Strong",
        )
        self.client.load_collection(name)

    @staticmethod
    def _row(kind: str, record: VectorRecord, vector: list[float]) -> dict[str, Any]:
        meta = record.metadata
        if kind == "knowledge":
            return {
                "vector_id": str(record.key),
                "vector": vector,
                "doc_id": int(meta["doc_id"]),
                "doc_type": str(meta["doc_type"]),
                "symbol": str(meta["symbol"]),
                "trust_level": str(meta["trust_level"]),
                "published_at": int(meta["published_at"]),
            }
        if kind == "memory":
            return {
                "id": int(record.key),
                "vector": vector,
                "user_id": int(meta["user_id"]),
                "content": record.text,
                "memory_type": str(meta["memory_type"]),
                "importance": float(meta["importance"]),
            }
        return {
            "id": int(record.key),
            "vector": vector,
            "status": str(meta["status"]),
            "tags": str(meta["tags"])[:4096],
            "market_env": str(meta["market_env"])[:4096],
        }

    def _count(self, name: str, expected: int) -> int:
        self.client.flush(collection_name=name, timeout=120)
        count = -1
        for _ in range(30):
            stats = self.client.get_collection_stats(name, timeout=10)
            count = int(stats.get("row_count") or stats.get("num_entities") or 0)
            if count == expected:
                return count
            time.sleep(1)
        return count

    def _current_count(self, name: str) -> int:
        stats = self.client.get_collection_stats(name, timeout=10)
        return int(stats.get("row_count") or stats.get("num_entities") or 0)

    def _smoke(
        self,
        kind: str,
        name: str,
        record: VectorRecord,
        vector: list[float],
        total: int,
    ) -> dict[str, Any]:
        primary = "vector_id" if kind == "knowledge" else "id"
        stored = self.client.get(
            collection_name=name,
            ids=[record.key],
            output_fields=[primary],
            consistency_level="Strong",
        )
        stored_ids = {
            str(row.get(primary) if row.get(primary) is not None else row.get("id"))
            for row in stored
        }
        if str(record.key) not in stored_ids:
            raise RuntimeError(f"{name}: exact primary-key read failed for {record.key}")

        result = self.client.search(
            collection_name=name,
            data=[vector],
            limit=min(50, total),
            output_fields=[primary],
            consistency_level="Strong",
        )
        hits = result[0] if result else []
        hit_ids = []
        for hit in hits:
            entity = hit.get("entity") or {}
            value = hit.get(primary)
            if value is None:
                value = hit.get("id")
            if value is None:
                value = entity.get(primary)
            hit_ids.append(str(value))
        if str(record.key) not in hit_ids:
            preview = [{"id": hit_ids[i], "distance": hits[i].get("distance")} for i in range(min(3, len(hits)))]
            raise RuntimeError(
                f"{name}: recall smoke test did not return source id {record.key}; top={preview}"
            )
        return {
            "smoke_ok": True,
            "smoke_expected": str(record.key),
            "smoke_top_ids": hit_ids[:3],
        }

    def migrate(
        self,
        kind: str,
        name: str,
        records: list[VectorRecord],
        embed: Callable[[list[str]], list[list[float]]],
        dim: int,
    ) -> dict[str, Any]:
        self._ensure_collection(kind, name, dim)
        started = time.monotonic()
        sample = max(records, key=lambda record: len(record.text)) if records else None

        current_count = self._current_count(name)
        if not self.reset and current_count == len(records):
            smoke = {"smoke_ok": True, "smoke_expected": None, "smoke_top_ids": []}
            if sample is not None:
                vectors = embed([sample.text])
                if len(vectors) != 1 or len(vectors[0]) != dim:
                    raise RuntimeError(f"{name}: embedding response shape mismatch during resume verification")
                smoke = self._smoke(kind, name, sample, vectors[0], len(records))
            return {
                "source_count": len(records),
                "target_count": current_count,
                "resumed": True,
                **smoke,
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }

        for offset in range(0, len(records), self.batch_size):
            batch = records[offset:offset + self.batch_size]
            vectors = embed([record.text for record in batch])
            if len(vectors) != len(batch) or any(len(vector) != dim for vector in vectors):
                raise RuntimeError(f"{name}: embedding response shape mismatch at offset {offset}")
            data = [self._row(kind, record, vector) for record, vector in zip(batch, vectors)]
            self.client.upsert(collection_name=name, data=data)
            done = offset + len(batch)
            if done == len(records) or done % 200 < self.batch_size:
                print(f"{name}: {done}/{len(records)}", file=sys.stderr, flush=True)

        target_count = self._count(name, len(records))
        if target_count != len(records):
            raise RuntimeError(f"{name}: row count mismatch source={len(records)} target={target_count}")

        smoke = {"smoke_ok": True, "smoke_expected": None, "smoke_top_ids": []}
        if sample is not None:
            vectors = embed([sample.text])
            if len(vectors) != 1 or len(vectors[0]) != dim:
                raise RuntimeError(f"{name}: embedding response shape mismatch during verification")
            smoke = self._smoke(kind, name, sample, vectors[0], len(records))

        return {
            "source_count": len(records),
            "target_count": target_count,
            "resumed": False,
            **smoke,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild MySQL vector data in Milvus")
    parser.add_argument("--uri", default=settings.milvus_uri)
    parser.add_argument("--reset", action="store_true", help="drop target collections before rebuilding")
    parser.add_argument("--batch-size", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    started = time.monotonic()
    migration: MilvusMigration | None = None
    try:
        sources, source_models = _load_sources()
        base_url, api_key, model_name = memory._resolve_embedding_cfg()
        if not (base_url and api_key and model_name):
            raise RuntimeError("remote embedding configuration is incomplete")
        embedder = memory._RemoteEmbeddingFunction(base_url=base_url, api_key=api_key, model=model_name)
        probe = embedder(["Milvus migration dimension probe"])
        if not probe or not probe[0]:
            raise RuntimeError("embedding probe returned no vector")
        dim = len(probe[0])

        migration = MilvusMigration(args.uri, settings.milvus_token, args.reset, max(1, args.batch_size))
        collection_specs = (
            ("knowledge", settings.rag_milvus_collection),
            ("memory", "user_memories"),
            ("experience", settings.experience_milvus_collection),
        )
        results = {
            name: migration.migrate(kind, name, sources[kind], embedder, dim)
            for kind, name in collection_specs
        }
        report = {
            "status": "ok",
            "embedding_model": model_name,
            "source_embedding_models": source_models,
            "dimension": dim,
            "collections": results,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        if migration is not None:
            migration.close()


if __name__ == "__main__":
    raise SystemExit(main())
