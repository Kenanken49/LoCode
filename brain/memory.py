"""Freedom — adaptive memory engine.

The whole point of this module: Freedom must run on *any* machine, from a 1 GB
VPS to a 128 GB workstation, and it must never be the reason the box OOMs.

It measures the host (RAM / swap / disk / cores), derives a safe budget, and
then sizes every cache, buffer and context window to that budget. Budgets are
re-derived continuously, so if another process eats the heap mid-session,
Freedom shrinks itself instead of crashing.
"""

from __future__ import annotations

import os
import sys
import time
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


# --------------------------------------------------------------------------- #
# Host probing
# --------------------------------------------------------------------------- #
def _linux_meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                kb = rest.strip().split(" ")[0]
                if kb.isdigit():
                    out[key] = int(kb) * 1024
    except OSError:
        pass
    return out


def _cgroup_limit() -> Optional[int]:
    """Containers lie about RAM. Respect the cgroup ceiling if there is one."""
    candidates = (
        "/sys/fs/cgroup/memory.max",                      # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",     # cgroup v1
    )
    for path in candidates:
        try:
            with open(path) as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw in ("", "max"):
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        # 2^63-ish sentinel means "unlimited"
        if 0 < value < 2**62:
            return value
    return None


@dataclass
class HostProfile:
    total_ram: int
    available_ram: int
    swap_total: int
    swap_free: int
    cores: int
    disk_total: int
    disk_free: int
    containerized: bool = False

    @property
    def ram_gb(self) -> float:
        return self.total_ram / 1e9

    def snapshot(self) -> Dict[str, Any]:
        g = 1e9
        return {
            "ram_total": round(self.ram_gb, 2),
            "ram_available": round(self.available_ram / g, 2),
            "swap_total": round(self.swap_total / g, 2),
            "swap_free": round(self.swap_free / g, 2),
            "cores": self.cores,
            "disk_total": round(self.disk_total / g, 2),
            "disk_free": round(self.disk_free / g, 2),
            "containerized": self.containerized,
        }


def probe_host(workspace: str = ".") -> HostProfile:
    total = available = swap_total = swap_free = 0
    mi = _linux_meminfo()
    if mi:
        total = mi.get("MemTotal", 0)
        available = mi.get("MemAvailable", mi.get("MemFree", 0))
        swap_total = mi.get("SwapTotal", 0)
        swap_free = mi.get("SwapFree", 0)
    else:  # macOS / Windows / anything exotic
        try:
            page = os.sysconf("SC_PAGE_SIZE")
            pages = os.sysconf("SC_PHYS_PAGES")
            avail_pages = os.sysconf("SC_AVPHYS_PAGES")
            total, available = page * pages, page * avail_pages
        except (ValueError, OSError, AttributeError):
            total = available = int(os.environ.get("FREEDOM_FAKE_RAM_GB", 4)) * 10**9
        swap_total = swap_free = 0

    limit = _cgroup_limit()
    containerized = False
    if limit:
        containerized = True
        total = min(total, limit)
        available = min(available, limit)

    cores = os.cpu_count() or 1
    disk_total = disk_free = 0
    try:
        if hasattr(os, "statvfs"):
            st = os.statvfs(os.path.abspath(workspace))
            disk_total, disk_free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        else:
            import shutil as _sh
            u = _sh.disk_usage(os.path.abspath(workspace))
            disk_total, disk_free = u.total, u.free
    except Exception:
        disk_total = disk_free = 0

    return HostProfile(total, available, swap_total, swap_free, cores, disk_total, disk_free, containerized)


# --------------------------------------------------------------------------- #
# Budgeting
# --------------------------------------------------------------------------- #
@dataclass
class MemoryBudget:
    """Everything Freedom is allowed to hold in RAM, derived from the host."""

    headroom: int = 0                 # bytes deliberately left for the OS
    platform_cap: int = 0             # bytes the app itself may allocate
    model_budget: int = 0             # bytes a local model may occupy
    context_tokens: int = 0           # tokens we keep in the live transcript
    history_messages: int = 0         # turns retained verbatim
    max_file_bytes: int = 0           # largest file we read into memory
    vector_store_items: int = 0       # embeddings kept resident
    tool_output_chars: int = 0        # truncation ceiling per tool result
    workspace_cache_items: int = 0    # parsed-file cache entries
    concurrency: int = 1              # parallel agent/tool workers
    tier: str = "tiny"                # tiny | small | medium | large | huge

    def as_dict(self) -> Dict[str, Any]:
        g = 1e9
        m = 1e6
        return {
            "tier": self.tier,
            "headroom_mb": round(self.headroom / m),
            "platform_cap_mb": round(self.platform_cap / m),
            "model_budget_gb": round(self.model_budget / g, 2),
            "context_tokens": self.context_tokens,
            "history_messages": self.history_messages,
            "max_file_kb": round(self.max_file_bytes / 1024),
            "vector_items": self.vector_store_items,
            "tool_output_chars": self.tool_output_chars,
            "workspace_cache_items": self.workspace_cache_items,
            "concurrency": self.concurrency,
        }


def compute_budget(host: HostProfile, reserve_gb: float = 0.0, prefer_local: bool = True) -> MemoryBudget:
    """Turn a host profile into a concrete budget.

    ``reserve_gb`` lets the user protect room for their own apps; the engine
    always keeps an additional safety margin so it can shrink on the fly.
    """
    avail = max(host.available_ram, 128 * 10**6)

    # Tier ladder — deliberately conservative at the bottom so 512 MB boxes work.
    if avail < 700 * 10**6:
        tier = "tiny"
    elif avail < 2 * 10**9:
        tier = "small"
    elif avail < 6 * 10**9:
        tier = "medium"
    elif avail < 20 * 10**9:
        tier = "large"
    else:
        tier = "huge"

    # Fraction of *available* RAM the platform may use.
    fractions = {"tiny": 0.30, "small": 0.38, "medium": 0.45, "large": 0.50, "huge": 0.55}
    cap = int(avail * fractions[tier])
    if reserve_gb > 0:
        cap = max(96 * 10**6, cap - int(reserve_gb * 10**9))
    headroom = max(64 * 10**6, avail - cap)

    # A ~7B Q4 model needs ≈4.5 GB; below that we push to the cloud/orchestration.
    weights = {"tiny": 0.0, "small": 0.0, "medium": 3.2 * 10**9, "large": 9 * 10**9, "huge": 24 * 10**9}
    model_budget = int(min(weights[tier], cap * 0.8)) if prefer_local else 0

    ctx = {"tiny": 1024, "small": 3072, "medium": 8192, "large": 24576, "huge": 65536}[tier]
    hist = {"tiny": 6, "small": 12, "medium": 24, "large": 48, "huge": 96}[tier]
    maxfile = {"tiny": 64 * 1024, "small": 256 * 1024, "medium": 10 ** 6, "large": 4 * 10**6, "huge": 16 * 10**6}[tier]
    vectors = {"tiny": 256, "small": 1024, "medium": 4096, "large": 20000, "huge": 100000}[tier]
    toolchars = {"tiny": 1500, "small": 3000, "medium": 6000, "large": 12000, "huge": 24000}[tier]
    cache = {"tiny": 32, "small": 96, "medium": 256, "large": 768, "huge": 2048}[tier]
    conc = max(1, min(host.cores, {"tiny": 1, "small": 2, "medium": 4, "large": 8, "huge": 16}[tier]))

    return MemoryBudget(headroom, cap, model_budget, ctx, hist, maxfile, vectors, toolchars, cache, conc, tier)


# --------------------------------------------------------------------------- #
# Bounded LRU store used for every cache in the product
# --------------------------------------------------------------------------- #
class BoundedStore:
    """LRU map capped by *both* item count and estimated bytes."""

    def __init__(self, name: str, max_items: int, max_bytes: int, on_evict=None):
        self.name = name
        self.max_items = max(1, max_items)
        self.max_bytes = max(1024, max_bytes)
        self._data: "OrderedDict[Any, Any]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.on_evict = on_evict

    @staticmethod
    def _size(value: Any) -> int:
        if isinstance(value, (str, bytes, bytearray)):
            return len(value)
        try:
            return len(json.dumps(value, default=str))
        except Exception:
            return 64

    def put(self, key: Any, value: Any) -> None:
        size = self._size(value)
        with self._lock:
            if key in self._data:
                self._bytes -= self._size(self._data.pop(key))
            self._data[key] = value
            self._bytes += size
            while (len(self._data) > self.max_items or self._bytes > self.max_bytes) and self._data:
                k, v = self._data.popitem(last=False)
                self._bytes -= self._size(v)
                self.evictions += 1
                if self.on_evict:
                    try:
                        self.on_evict(k, v)
                    except Exception:
                        pass

    def get(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self.hits += 1
                return self._data[key]
            self.misses += 1
            return default

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._bytes = 0

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "items": len(self._data),
                "bytes": self._bytes,
                "max_items": self.max_items,
                "max_bytes": self.max_bytes,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
            }


# --------------------------------------------------------------------------- #
# The conversation memory that actually gets trimmed
# --------------------------------------------------------------------------- #
@dataclass
class MemoryItem:
    role: str
    content: str
    ts: float = field(default_factory=time.time)
    kind: str = "chat"          # chat | tool | summary | retrieval
    tokens: int = 0
    pinned: bool = False

    def to_message(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate (~3.6 chars/token for code-heavy text)."""
    if not text:
        return 0
    return max(1, int(len(text) / 3.6))


class ConversationMemory:
    """Token-budgeted transcript with automatic summarisation of what falls out.

    When the live window overflows, the oldest unpinned turns are compressed
    into a rolling summary instead of being dropped — so a 1 GB laptop still
    keeps the *gist* of a long session, while a big machine keeps everything
    verbatim.
    """

    def __init__(self, budget: MemoryBudget, summarizer=None):
        self.budget = budget
        self.items: List[MemoryItem] = []
        self.summary: str = ""
        self.summarizer = summarizer      # callable(list[MemoryItem]) -> str
        self.compressions = 0
        self._lock = threading.RLock()

    # -- write -------------------------------------------------------------- #
    def add(self, role: str, content: str, kind: str = "chat", pinned: bool = False) -> MemoryItem:
        item = MemoryItem(role=role, content=content, kind=kind, tokens=estimate_tokens(content), pinned=pinned)
        with self._lock:
            self.items.append(item)
            self._trim()
        return item

    # -- read --------------------------------------------------------------- #
    def messages(self) -> List[Dict[str, str]]:
        with self._lock:
            out: List[Dict[str, str]] = []
            if self.summary:
                out.append({"role": "system", "content": "Summary of earlier conversation:\n" + self.summary})
            out.extend(i.to_message() for i in self.items)
            return out

    def total_tokens(self) -> int:
        with self._lock:
            return sum(i.tokens for i in self.items) + estimate_tokens(self.summary)

    def tail(self, n: int = 8) -> List[MemoryItem]:
        with self._lock:
            return list(self.items[-n:])

    def clear(self) -> None:
        with self._lock:
            self.items.clear()
            self.summary = ""

    # -- pressure ----------------------------------------------------------- #
    def _trim(self) -> None:
        limit = self.budget.context_tokens
        overflow = self.total_tokens() - limit
        if overflow <= 0 and len(self.items) <= self.budget.history_messages * 3:
            return

        evictable = [i for i in self.items if not i.pinned]
        if not evictable:
            return
        need = max(1, len(evictable) // 3)
        chunk, self.items = evictable[:need], [i for i in self.items if i not in evictable[:need]]
        self.compressions += 1

        text = "\n".join(f"{m.role}: {m.content[:400]}" for m in chunk)
        if self.summarizer:
            try:
                fresh = self.summarizer(chunk)
            except Exception:
                fresh = text[:600]
        else:
            fresh = self._naive_summary(chunk)
        self.summary = (self.summary + "\n" + fresh).strip()[-2000:]

    @staticmethod
    def _naive_summary(items: Iterable[MemoryItem]) -> str:
        lines: List[str] = []
        for m in items:
            first = (m.content or "").strip().split("\n")[0][:160]
            if not first:
                continue
            tag = {"tool": "tool result", "retrieval": "context", "summary": "note"}.get(m.kind, m.role)
            lines.append(f"- [{tag}] {first}")
        return "\n".join(lines[:12]) or "- (earlier turns compressed)"

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "turns": len(self.items),
                "tokens": self.total_tokens(),
                "budget_tokens": self.budget.context_tokens,
                "summary_chars": len(self.summary),
                "compressions": self.compressions,
            }


# --------------------------------------------------------------------------- #
# Long-lived knowledge: project facts + scratchpad, persisted to disk
# --------------------------------------------------------------------------- #
class KnowledgeStore:
    """Disk-backed, RAM-capped fact store (the 'what I learned about this repo' layer)."""

    def __init__(self, path: str, budget: MemoryBudget):
        self.path = path
        self.budget = budget
        self.store = BoundedStore("knowledge", budget.vector_store_items, budget.platform_cap // 4)
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        for key, value in (data.get("facts") or {}).items():
            self.store.put(key, value)
        self.scratch = data.get("scratch", "")

    def remember(self, key: str, value: Any) -> None:
        with self._lock:
            self.store.put(key, value)

    def recall(self, key: str, default: Any = None) -> Any:
        return self.store.get(key, default)

    def all_facts(self) -> Dict[str, Any]:
        return dict(getattr(self.store, "_data", {}))

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            payload = {"facts": self.all_facts(), "scratch": getattr(self, "scratch", ""), "saved_at": time.time()}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1, default=str)
            os.replace(tmp, self.path)

    def stats(self) -> Dict[str, Any]:
        s = self.store.stats()
        s["path"] = self.path
        return s


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #
class MemoryEngine:
    """Single object the rest of Freedom asks about memory."""

    def __init__(self, workspace: str, reserve_gb: float = 0.0, prefer_local: bool = True, state_dir: Optional[str] = None):
        self.workspace = os.path.abspath(workspace)
        self.state_dir = state_dir or os.path.join(self.workspace, ".freedom")
        self.reserve_gb = reserve_gb
        self.prefer_local = prefer_local
        self.host = probe_host(self.workspace)
        self.budget = compute_budget(self.host, reserve_gb, prefer_local)
        self.conversation = ConversationMemory(self.budget)
        self.knowledge = KnowledgeStore(os.path.join(self.state_dir, "memory.json"), self.budget)
        self.files = BoundedStore("file-cache", self.budget.workspace_cache_items, self.budget.platform_cap // 3)
        self.web = BoundedStore("web-cache", 64, self.budget.platform_cap // 6)
        self._pressure_log: List[Dict[str, Any]] = []

    # -- dynamic re-sizing --------------------------------------------------- #
    def refresh(self) -> Dict[str, Any]:
        """Re-probe the host and resize every store. Called on a timer + before big ops."""
        old_tier = self.budget.tier
        self.host = probe_host(self.workspace)
        self.budget = compute_budget(self.host, self.reserve_gb, self.prefer_local)
        self.conversation.budget = self.budget
        self.files.max_items = self.budget.workspace_cache_items
        self.files.max_bytes = self.budget.platform_cap // 3
        self.web.max_items = 64
        self.knowledge.store.max_items = self.budget.vector_store_items
        if self.budget.tier != old_tier:
            self._pressure_log.append({"ts": time.time(), "from": old_tier, "to": self.budget.tier,
                                       "available_mb": round(self.host.available_ram / 1e6)})
        return self.snapshot()

    def usage_ratio(self) -> float:
        used = sum(s["bytes"] for s in (self.files.stats(), self.web.stats(), self.knowledge.stats()))
        return round(used / max(1, self.budget.platform_cap), 4)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "host": self.host.snapshot(),
            "budget": self.budget.as_dict(),
            "conversation": self.conversation.stats(),
            "usage_ratio": self.usage_ratio(),
            "stores": [self.files.stats(), self.web.stats(), self.knowledge.stats()],
            "events": self._pressure_log[-10:],
            "python": sys.version.split()[0],
        }

    def fit_model(self, needed_bytes: int) -> bool:
        """Can this box actually hold a model of that weight size?"""
        return needed_bytes <= self.budget.model_budget

    def save(self) -> None:
        self.knowledge.save()
