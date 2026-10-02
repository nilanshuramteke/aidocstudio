"""Composition root: wires storage, queue, workers and providers. The only place concrete types meet."""
import time
from dataclasses import dataclass, field

from ..documents.pipeline import make_paginate_handler
from ..documents.extract_stage import make_classify_handler, make_extract_handler, make_validate_handler
from ..documents.bulk import BulkService
from ..export.service import ExportService
from ..ingestion.watcher import WatchService
from ..knowledge.service import KnowledgeService
from ..ops.backup import apply_pending_restore, create_backup, prune_auto_backups
from ..ops.scheduler import BackupScheduler
from ..workflows.engine import WorkflowService
from ..documents.review import ReviewService
from ..organization.service import OrganizationService
from ..documents.service import DocumentService
from ..documents.text_stage import make_text_handler
from ..documents.types import DocumentTypes, ensure_seed
from ..ai.providers.ollama import OllamaEmbedding, OllamaLLM
from ..ai.compare import CompareService
from ..ai.rag import RagService
from ..search.service import SearchService
from ..search.stages import make_embed_handler, make_fields_handler, make_index_handler
from ..search.vector import VectorIndex
from ..ocr.registry import choose_engine, make_provider
from ..ocr.runner import OCRRunner
from ..jobs.queue import JobQueue
from ..jobs.worker import Handler, JobContext, WorkerPool
from ..storage import crypt
from ..storage.db import Database, load_sqlite_vec, probe_capabilities, vec_available
from ..storage.filestore import FileStore
from ..storage.settings import SettingsStore
from .applock import AppLock
from .config import Config
from .events import EventBus
from .interfaces import EmbeddingProvider, LLMProvider, OCRProvider
from .security import SessionAuth


def _debug_sleep(ctx: JobContext) -> None:
    """Dummy job for crash-recovery tests: sleeps in small steps, then writes an optional marker file."""
    seconds = float(ctx.job.payload.get("seconds", 0))
    if ctx.job.payload.get("slow_first") and ctx.job.attempts > 1:
        seconds = 0  # simulates a hang on the first attempt only
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(0.05, max(0.0, end - time.monotonic())))
    marker = ctx.job.payload.get("marker")
    if marker:
        with open(marker, "a", encoding="utf-8") as f:
            f.write(ctx.job.id + "\n")


@dataclass
class Container:
    config: Config
    db: Database
    bus: EventBus
    queue: JobQueue
    pool: WorkerPool
    settings: SettingsStore
    auth: SessionAuth
    capabilities: dict
    files: FileStore
    docs: DocumentService
    ocr_runner: OCRRunner | None = None
    types: DocumentTypes | None = None
    search: SearchService | None = None
    rag: RagService | None = None
    compare: CompareService | None = None
    review: ReviewService | None = None
    org: OrganizationService | None = None
    bulk: BulkService | None = None
    exporter: ExportService | None = None
    workflows: WorkflowService | None = None
    watch: WatchService | None = None
    scheduler: BackupScheduler | None = None
    knowledge: KnowledgeService | None = None
    applock: AppLock | None = None
    ocr: OCRProvider | None = None
    llm: LLMProvider | None = None
    embedding: EmbeddingProvider | None = None
    handlers: dict[str, Handler] = field(default_factory=dict)

    def start(self) -> None:
        self.pool.start()
        if self.watch:
            self.watch.start(float(self.settings.all()["watch.scan_interval_s"]))
        if self.scheduler:
            self.scheduler.start()

    def stop(self) -> None:
        if self.watch:
            self.watch.stop()
        if self.scheduler:
            self.scheduler.stop()
        self.pool.stop()
        if self.ocr_runner:
            self.ocr_runner.close()
        self.db.close()


def build_container(config: Config, *, ocr=None, llm=None, embedding=None,
                    auth: SessionAuth | None = None, start_workers: bool = True,
                    auto_llm: bool = True, key: bytes | None = None) -> Container:
    caps = probe_capabilities()
    config.ensure_dirs()
    caps["restored_from_backup"] = apply_pending_restore(config.data_root)  # must happen before the DB is opened
    caps["vec"] = vec_available()  # sqlite-vec loadable? else semantic search degrades to keyword-only
    caps["encrypted"] = crypt.is_encrypted(config.data_root)
    if caps["encrypted"] and key is None:
        key = crypt.get_key(config.data_root)  # env var / OS keychain; raises AppError(locked) when neither is available
    db = Database(config.db_path, on_connect=[load_sqlite_vec] if caps["vec"] else [], key=key if caps["encrypted"] else None)
    db.migrate(backup_dir=config.data_root / "backups")
    bus = EventBus()
    queue = JobQueue(db, bus)
    handlers: dict[str, Handler] = {"debug:sleep": _debug_sleep}
    files = FileStore(config.data_root)
    docs = DocumentService(db, files, queue, bus)
    settings = SettingsStore(db)
    cfg = settings.all()
    if ocr is not None:  # injected provider (tests/fakes) runs in-process
        runner = OCRRunner(ocr.name, inline=ocr)
    else:
        engine = choose_engine(cfg["ocr.engine"])
        runner = OCRRunner(engine, workers=min(2, config.workers), timeout_s=cfg["ocr.timeout_s"]) if engine else None
        ocr = make_provider(engine) if engine else None
    ensure_seed(db)
    types = DocumentTypes(db)
    if llm is None and auto_llm and cfg["llm.model"]:
        llm = OllamaLLM(cfg["llm.base_url"], cfg["llm.model"])  # health is checked lazily; failures degrade gracefully
    if embedding is None and auto_llm and cfg["embedding.model"] and caps["vec"]:
        embedding = OllamaEmbedding(cfg["llm.base_url"], cfg["embedding.model"])
    search = SearchService(db, VectorIndex(db) if caps["vec"] else None, embedding if caps["vec"] else None)
    org = OrganizationService(db, search)
    review = ReviewService(docs, types, settings, search, runner, llm)
    exporter = ExportService(db, config.data_root / "exports")
    workflows = WorkflowService(db, org, exporter, settings, bus, docs, types)
    watch = WatchService(db, docs, config.data_root)
    scheduler = BackupScheduler(config.data_root, settings, queue)

    def dispatch_hook(event: str, doc_id: str) -> None:
        if workflows.listens_to(event):  # cheap guard: no job unless some enabled rule wants this event
            queue.enqueue("workflow:dispatch", {"event": event}, document_id=doc_id, priority=6)

    docs.event_hooks.append(dispatch_hook)
    knowledge = KnowledgeService(db, types)
    handlers["stage:entities"] = lambda ctx: knowledge.link_document(ctx.job.document_id)
    docs.event_hooks.append(lambda ev, doc: queue.enqueue("stage:entities", document_id=doc, priority=7)
                            if ev in ("document.deleted", "document.restored") else None)
    handlers["workflow:dispatch"] = lambda ctx: workflows.dispatch(ctx.job.payload["event"], ctx.job.document_id)

    def backup_job(ctx):
        create_backup(config.data_root, db, auto=bool(ctx.job.payload.get("auto")))
        prune_auto_backups(config.data_root, int(settings.all()["backup.keep"]))

    handlers["backup"] = backup_job
    docs.after_text = ["stage:classify", "stage:index"]  # index right after text so docs are keyword-searchable early
    docs.after_validate = ["stage:index_fields", "stage:entities"]
    docs.after_review = ["stage:entities"]
    handlers["stage:index"] = make_index_handler(search, docs)
    handlers["stage:embed"] = make_embed_handler(search, docs)
    handlers["stage:index_fields"] = make_fields_handler(search)
    handlers["stage:paginate"] = make_paginate_handler(docs)
    handlers["stage:classify"] = make_classify_handler(docs, types, llm)
    handlers["stage:extract"] = make_extract_handler(docs, types, settings, llm)
    handlers["stage:validate"] = make_validate_handler(docs, types, settings)
    handlers["stage:text"] = make_text_handler(docs, settings, runner)
    pool = WorkerPool(queue, handlers, workers=config.workers,
                      heartbeat_s=config.heartbeat_s, stale_after_s=config.stale_after_s)
    c = Container(config, db, bus, queue, pool, settings, auth or SessionAuth(), caps, files, docs,
                  ocr_runner=runner, types=types, search=search,
                  rag=RagService(db, search, llm, org.document_ids), compare=CompareService(search, llm),
                  review=review, org=org, bulk=BulkService(docs, review, org, exporter), exporter=exporter,
                  workflows=workflows, watch=watch, scheduler=scheduler, knowledge=knowledge,
                  applock=AppLock(config.data_root), ocr=ocr, llm=llm, embedding=embedding, handlers=handlers)
    if start_workers:
        c.start()
    return c
