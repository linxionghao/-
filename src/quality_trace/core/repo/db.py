"""数据层：SQLite 连接与会话/消息/引用三表的读写。

【核心模块】对应 docs/architecture.md 3.1 节的扩展表（经 2026-10-30 决策确认）：
    sys_user               用户（三角色 + 产线绑定，阶段 3 权限基座）
    chat_session           会话
    chat_message           消息（含降级标记）
    chat_message_citation  引用（结构化落库，可单独审计）
    defect_item            缺陷单（双时间 + 补录标记 + 离线幂等键）
    defect_code            缺陷码字典（描述归一化的候选来源）
    attribution_order      归因单（审批状态机 + 8D 草稿缓存）

设计约定：
- 沿用项目双时间列约定：occurred_at（业务时刻）/ recorded_at（入库时刻）；
- 写入策略按决策 D①：消息先写 partial/complete，允许 UPDATE 改状态与正文；
- 全部函数接收 conn 参数，由调用方管理事务，便于单测用临时库；
- core 约束：不 print、不读环境变量、不 import argparse。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_DB_PATH = "data/trace.db"

# ---- 建表语句（幂等：IF NOT EXISTS）----
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sys_user (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL,                     -- quality_engineer / shift_leader / quality_manager
    line_id       TEXT,                              -- 班组长绑定产线；QE/QM 可为 NULL（跨线）
    status        TEXT NOT NULL DEFAULT 'active',    -- active / disabled
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_doc (
    doc_id          TEXT PRIMARY KEY,
    doc_name        TEXT NOT NULL,                   -- 不含扩展名的文档名
    doc_type        TEXT NOT NULL,                   -- standard / sop_manual / 8d_case
    version         TEXT NOT NULL,                   -- 版本号（上传时必填）
    effective_from  TEXT NOT NULL,                   -- 生效区间起（上传时必填）
    effective_to    TEXT NOT NULL,                   -- 生效区间止（上传时必填）
    status          TEXT NOT NULL DEFAULT 'active',  -- active / archived（下架≠删除）
    source_path     TEXT,                           -- 原始文件保存路径（md/pdf）
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    vector_status   TEXT NOT NULL DEFAULT 'pending', -- pending / ok
    uploaded_by     TEXT,
    occurred_at     TEXT NOT NULL,                   -- 业务时间：上传时刻
    recorded_at     TEXT NOT NULL,
    archived_at     TEXT,
    archived_by     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_doc_name_version
    ON kb_doc(doc_name, version);                    -- 同名同版本唯一（40903 冲突检测）
CREATE INDEX IF NOT EXISTS idx_kb_doc_status
    ON kb_doc(status, doc_type);

CREATE TABLE IF NOT EXISTS kb_chunk (
    chunk_id     TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL REFERENCES kb_doc(doc_id),
    seq_no       INTEGER NOT NULL,                   -- 文档内切片序号
    content      TEXT NOT NULL,
    clause       TEXT,                               -- 所在标题（引用溯源定位用）
    char_len     INTEGER NOT NULL,
    occurred_at  TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_doc
    ON kb_chunk(doc_id, seq_no);

CREATE TABLE IF NOT EXISTS prompt_template (
    template_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    content      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',     -- 全库仅一条 active
    updated_by   TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_template_history (
    history_id   TEXT PRIMARY KEY,
    template_id  TEXT NOT NULL REFERENCES prompt_template(template_id),
    content      TEXT NOT NULL,
    updated_by   TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_session (
    session_id       TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    role             TEXT NOT NULL,
    title            TEXT,
    status           TEXT NOT NULL DEFAULT 'active',   -- active / archived
    message_count    INTEGER NOT NULL DEFAULT 0,
    occurred_at      TEXT NOT NULL,                    -- 业务时间：会话创建
    recorded_at      TEXT NOT NULL,                    -- 入库时间
    last_message_at  TEXT
);

CREATE TABLE IF NOT EXISTS chat_message (
    message_id       TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES chat_session(session_id),
    role             TEXT NOT NULL,                    -- user / assistant
    content          TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'complete', -- complete / partial / failed
    degraded         INTEGER NOT NULL DEFAULT 0,       -- 0/1：是否走了降级
    degraded_code    TEXT,                             -- 50001 / 50002 / 50401
    degraded_reason  TEXT,                             -- 中文降级说明
    retrieved_json   TEXT,                             -- 本次命中的切片与分数（审计）
    query_id         TEXT,                             -- 关联 retrieval_log
    model            TEXT,
    prompt_hash      TEXT,
    latency_ms       INTEGER,
    occurred_at      TEXT NOT NULL,
    recorded_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_message_citation (
    citation_id  TEXT PRIMARY KEY,
    message_id   TEXT NOT NULL REFERENCES chat_message(message_id),
    seq          INTEGER NOT NULL,
    source_file  TEXT NOT NULL,                        -- 文档名
    doc_id       TEXT,
    clause       TEXT,                                 -- 条款号 / 章节
    version      TEXT,                                 -- 版本号（缺失时为 NULL）
    doc_type     TEXT,                                 -- standard / sop_manual / 8d_case
    chunk_id     TEXT,
    score        REAL,
    snippet      TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_message_session
    ON chat_message(session_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_citation_message
    ON chat_message_citation(message_id, seq);

-- ---- 批次 1c：归因审批域（工单/缺陷为简表，谱系全表留阶段 4）----
CREATE TABLE IF NOT EXISTS work_order (
    work_order_id  TEXT PRIMARY KEY,
    product_name  TEXT NOT NULL,
    line_id       TEXT NOT NULL,                       -- 数据级权限过滤的锚点
    status        TEXT NOT NULL DEFAULT 'open',
    planned_qty   INTEGER NOT NULL DEFAULT 0,
    occurred_at   TEXT NOT NULL,
    recorded_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS defect_item (
    defect_no       TEXT PRIMARY KEY,
    work_order_id   TEXT NOT NULL REFERENCES work_order(work_order_id),
    line_id         TEXT NOT NULL,
    op_code         TEXT,                               -- 工序码
    description     TEXT NOT NULL,
    qty             INTEGER NOT NULL DEFAULT 1,
    severity        TEXT,                               -- 轻微 / 一般 / 严重
    defect_code     TEXT,                               -- draft 阶段为空；确认后才写入
    suggest_json    TEXT,                               -- 候选码（LLM/关键词来源，审计留痕）
    photo_path      TEXT,
    client_draft_id TEXT UNIQUE,                       -- 离线补传幂等键
    status          TEXT NOT NULL DEFAULT 'draft',     -- draft / pending（judged 属阶段 4）
    is_backfill     INTEGER NOT NULL DEFAULT 0,        -- 补录标记（双时间差 >60s）
    occurred_at     TEXT NOT NULL,                     -- 业务时间（现场发生）
    recorded_at     TEXT NOT NULL,                     -- 录入时间（落库）
    created_by      TEXT,
    confirmed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_defect_line
    ON defect_item(line_id, occurred_at);

CREATE TABLE IF NOT EXISTS attribution_order (
    attribution_id  TEXT PRIMARY KEY,
    defect_no       TEXT NOT NULL REFERENCES defect_item(defect_no),
    evidence_json   TEXT NOT NULL,                      -- 结构化证据链（规则命中/统计线索/引用切片）
    hypothesis_text TEXT NOT NULL,                      -- L3 因果假设，恒标「待验证」
    status          TEXT NOT NULL DEFAULT 'submitted', -- submitted / approved / rejected
    draft_8d_md     TEXT,                               -- 8D 草稿缓存（生成后落库）
    submitted_by    TEXT,
    submitted_at    TEXT NOT NULL,
    decided_by      TEXT,                               -- 审批人（主管）
    decided_at      TEXT,
    decision_note   TEXT                                -- 退回必填 / 批准可附
);
CREATE INDEX IF NOT EXISTS idx_attr_status
    ON attribution_order(status, submitted_at);

-- ---- 批次 2：缺陷码字典（描述归一化的候选来源，运营可在后台维护）----
CREATE TABLE IF NOT EXISTS defect_code (
    code          TEXT PRIMARY KEY,                    -- 缺陷码，如 DC-012
    name          TEXT NOT NULL,                       -- 中文名，如「螺纹外径偏小」
    category      TEXT NOT NULL DEFAULT '尺寸',        -- 分组：尺寸/外观/装配/材料/其他
    keywords_json TEXT NOT NULL DEFAULT '[]',          -- 关键词回退匹配用（无 LLM 时的兜底）
    status        TEXT NOT NULL DEFAULT 'active',      -- active / disabled
    sort_no       INTEGER NOT NULL DEFAULT 0
);

-- ---- 阶段 4：计量型实测值（SPC 的数据源）----
-- 为什么单开一张表、而不是给 defect_item 加一列「实测值」：
--   ① 缺陷单只记「不良」，而控制图要的是**合格件也测**的连续数据——
--      只拿不良品的数据画控制图，等于只看病人算平均体温；
--   ② 一个工单一次抽检 5 件，是 5 行，不是 1 行；
--   ③ 量具、测量人、子组号都要留痕，塞进缺陷单会把它撑成宽表。
CREATE TABLE IF NOT EXISTS meas_record (
    record_id     TEXT PRIMARY KEY,
    item_code     TEXT NOT NULL,                       -- 检验项目，如「外径」
    work_order_id TEXT,
    line_id       TEXT NOT NULL,
    op_code       TEXT,
    subgroup_no   INTEGER NOT NULL DEFAULT 1,          -- 子组序号（同一次抽检的 5 件同号）
    value         REAL NOT NULL,                       -- 实测值
    unit          TEXT NOT NULL DEFAULT 'mm',
    usl           REAL,                                -- 规格上限（可空：只做控制图不做能力）
    lsl           REAL,
    gauge         TEXT,                                -- 量具编号（追溯量具误差用）
    operator      TEXT,
    occurred_at   TEXT NOT NULL,                       -- 业务时间：测量时刻
    recorded_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meas_item_time
    ON meas_record(item_code, occurred_at);
CREATE INDEX IF NOT EXISTS idx_meas_line_time
    ON meas_record(line_id, occurred_at);

-- ---- 阶段 5：批次谱系（追溯的骨架）----
-- 制造业追溯的本质是"物料批 × 工单"这张多对多表：
--   一批料可能用在多张工单上（正查：这批料害了多少单）；
--   一张工单可能吃多批料（反查：这单不良是哪批料造成的）。
-- 只建这两张表、不建"批次树"，是因为真实产线是**网状**不是树状：
-- 同一批钢卷可能先进冲压、再分流到两条装配线，树结构表达不了"分流"。
CREATE TABLE IF NOT EXISTS material_lot (
    lot_id        TEXT PRIMARY KEY,                    -- 内部批号，如 LOT-20261101-001
    material_code TEXT NOT NULL,                       -- 物料编码
    material_name TEXT NOT NULL,
    supplier      TEXT,
    supplier_lot  TEXT,                                -- 供应商批号（对外的追溯锚点）
    qty           REAL NOT NULL DEFAULT 0,
    unit          TEXT NOT NULL DEFAULT 'kg',
    status        TEXT NOT NULL DEFAULT 'in_use',      -- in_use / consumed / quarantined
    occurred_at   TEXT NOT NULL,                       -- 到货/投产时刻（业务时间）
    recorded_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lot_material ON material_lot(material_code, occurred_at);
CREATE INDEX IF NOT EXISTS idx_lot_supplier ON material_lot(supplier);

CREATE TABLE IF NOT EXISTS lot_consumption (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id        TEXT NOT NULL REFERENCES material_lot(lot_id),
    work_order_id TEXT NOT NULL REFERENCES work_order(work_order_id),
    qty_used      REAL NOT NULL DEFAULT 0,
    op_code       TEXT,
    occurred_at   TEXT NOT NULL,
    recorded_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lot_consume
    ON lot_consumption(lot_id, work_order_id);         -- 同一批料在同一工单只记一次
CREATE INDEX IF NOT EXISTS idx_consume_wo ON lot_consumption(work_order_id);

-- ---- 阶段 6：MSA 测量系统分析记录 ----
-- 为什么要落库、而不是每次现算：
--   MSA 的结论是**有时点的证据**。PPAP 文件里写着"2026-11-06 %GRR=8.7% 合格"，
--   半年后有人重跑一遍、发现数据变了，如果没有快照就没法解释当时为什么签字。
--   result_json 存的是计算结果的完整快照，raw_json 存原始读数——
--   前者用于复核结论，后者用于换算法重算，两者都不能只留一个。
CREATE TABLE IF NOT EXISTS msa_study (
    study_id      TEXT PRIMARY KEY,
    item_code     TEXT NOT NULL,                    -- 检验项目
    gauge         TEXT,                             -- 量具编号
    line_id       TEXT,
    tolerance     REAL,                             -- 公差带宽（USL-LSL），可空
    design_json   TEXT NOT NULL DEFAULT '{}',       -- 评价人数/零件数/试测次数
    raw_json      TEXT NOT NULL DEFAULT '[]',       -- 原始读数（可重算）
    result_json   TEXT NOT NULL DEFAULT '{}',       -- 计算结论快照（复核用）
    verdict       TEXT NOT NULL DEFAULT 'unknown',  -- accept / conditional / reject
    pct_grr       REAL,
    ndc           REAL,
    source        TEXT NOT NULL DEFAULT 'manual',   -- manual / measurements
    created_by    TEXT,
    occurred_at   TEXT NOT NULL,                    -- 研究时刻（业务时间）
    recorded_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msa_item ON msa_study(item_code, occurred_at);
"""

# ---- 增列迁移（SQLite 无 ADD COLUMN IF NOT EXISTS，逐条 try）----
# 既有库（阶段 2 建的 chat_message）升级到批次 1c 的审计标记列。
_MIGRATION_SQL = [
    "ALTER TABLE chat_message ADD COLUMN flag_root_cause INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chat_message ADD COLUMN flag_no_citation INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chat_message ADD COLUMN flag_stale_version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chat_message ADD COLUMN reviewed_by TEXT",
    "ALTER TABLE chat_message ADD COLUMN reviewed_at TEXT",
    "ALTER TABLE chat_message ADD COLUMN reviewed_note TEXT",
    "ALTER TABLE chat_session ADD COLUMN batch_no TEXT",
]


def _now() -> str:
    """当前时刻（ISO，秒级）。"""
    return _dt.datetime.now().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 id（便于日志辨认）。"""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """建立连接：WAL 模式 + 行对象访问 + 外键约束开启。"""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row          # 结果可按列名访问
    conn.execute("PRAGMA journal_mode=WAL")  # 读写并发友好
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """建表 + 增列迁移（全部幂等），可重复调用。"""
    conn.executescript(SCHEMA_SQL)
    for sql in _MIGRATION_SQL:
        try:
            conn.execute(sql)        # 列已存在时抛 OperationalError，忽略即可
        except sqlite3.OperationalError:
            pass
    conn.commit()


# ================================================================= 会话
def create_session(
    conn: sqlite3.Connection, user_id: str, role: str, title: str = ""
) -> Dict[str, Any]:
    """新建会话，返回会话行。"""
    session_id = new_id("sess")
    now = _now()
    conn.execute(
        "INSERT INTO chat_session"
        " (session_id, user_id, role, title, status, message_count,"
        "  occurred_at, recorded_at, last_message_at)"
        " VALUES (?,?,?,?, 'active', 0, ?,?,?)",
        (session_id, user_id, role, title, now, now, now),
    )
    conn.commit()
    return get_session(conn, session_id) or {}


def get_session(conn: sqlite3.Connection, session_id: str) -> Optional[Dict[str, Any]]:
    """按 id 取会话；不存在返回 None。"""
    row = conn.execute(
        "SELECT * FROM chat_session WHERE session_id = ?", (session_id,)
    ).fetchone()
    return dict(row) if row else None


def touch_session(conn: sqlite3.Connection, session_id: str, delta: int = 1) -> None:
    """更新会话的最后消息时间与消息计数。"""
    conn.execute(
        "UPDATE chat_session SET last_message_at = ?,"
        " message_count = message_count + ? WHERE session_id = ?",
        (_now(), delta, session_id),
    )
    conn.commit()


def archive_session(conn: sqlite3.Connection, session_id: str) -> bool:
    """归档会话；返回是否命中（False = 会话不存在）。"""
    cur = conn.execute(
        "UPDATE chat_session SET status = 'archived' WHERE session_id = ?"
        " AND status = 'active'",
        (session_id,),
    )
    conn.commit()
    return cur.rowcount > 0


# ================================================================= 消息
def save_message(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str,
    *,
    status: str = "complete",
    degraded: bool = False,
    degraded_code: Optional[str] = None,
    degraded_reason: Optional[str] = None,
    retrieved_json: Optional[str] = None,
    query_id: Optional[str] = None,
    model: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    latency_ms: Optional[int] = None,
) -> str:
    """写入一条消息，返回 message_id。

    流式场景：assistant 消息先以 status='partial' 写入（保证断流不丢），
    流结束后用 update_message 补齐正文与状态（决策 D①）。
    """
    message_id = new_id("msg")
    now = _now()
    conn.execute(
        "INSERT INTO chat_message"
        " (message_id, session_id, role, content, status, degraded,"
        "  degraded_code, degraded_reason, retrieved_json, query_id,"
        "  model, prompt_hash, latency_ms, occurred_at, recorded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            message_id, session_id, role, content, status,
            int(degraded), degraded_code, degraded_reason,
            retrieved_json, query_id, model, prompt_hash, latency_ms,
            now, now,
        ),
    )
    conn.commit()
    return message_id


def update_message(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    content: Optional[str] = None,
    status: Optional[str] = None,
    degraded: Optional[bool] = None,
    degraded_code: Optional[str] = None,
    degraded_reason: Optional[str] = None,
    retrieved_json: Optional[str] = None,
    model: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    latency_ms: Optional[int] = None,
) -> None:
    """按传入的非空字段更新消息（流式结束后补齐）。"""
    fields, values = [], []
    for column, value in (
        ("content", content), ("status", status),
        ("degraded", None if degraded is None else int(degraded)),
        ("degraded_code", degraded_code),
        ("degraded_reason", degraded_reason),
        ("retrieved_json", retrieved_json),
        ("model", model), ("prompt_hash", prompt_hash), ("latency_ms", latency_ms),
    ):
        if value is not None:
            fields.append(f"{column} = ?")
            values.append(value)
    if not fields:
        return
    values.append(message_id)
    conn.execute(
        f"UPDATE chat_message SET {', '.join(fields)} WHERE message_id = ?", values
    )
    conn.commit()


def list_messages(
    conn: sqlite3.Connection, session_id: str, limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """取会话消息（按业务时间正序），limit 为条数上限。"""
    sql = ("SELECT * FROM chat_message WHERE session_id = ?"
           " ORDER BY occurred_at ASC, rowid ASC")
    if limit:
        sql += " LIMIT ?"
        rows = conn.execute(sql, (session_id, limit)).fetchall()
    else:
        rows = conn.execute(sql, (session_id,)).fetchall()
    return [dict(r) for r in rows]


def count_recent_messages(
    conn: sqlite3.Connection, session_id: str, seconds: int
) -> int:
    """统计最近 N 秒内的消息数（用于限流 42901）。"""
    since = (_dt.datetime.now() - _dt.timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM chat_message"
        " WHERE session_id = ? AND occurred_at >= ?",
        (session_id, since),
    ).fetchone()
    return int(row["n"]) if row else 0


# ================================================================= 用户
def create_user(
    conn: sqlite3.Connection,
    username: str,
    password_hash: str,
    display_name: str,
    role: str,
    line_id: Optional[str] = None,
) -> Dict[str, Any]:
    """新建用户（幂等：同名用户已存在则直接返回既有行，供 seed 脚本重复执行）。"""
    existing = get_user_by_username(conn, username)
    if existing is not None:
        return existing
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO sys_user"
        " (user_id, username, password_hash, display_name, role, line_id,"
        "  status, created_at) VALUES (?,?,?,?,?,?, 'active', ?)",
        (user_id, username, password_hash, display_name, role, line_id, _now()),
    )
    conn.commit()
    return get_user(conn, user_id) or {}


def get_user(conn: sqlite3.Connection, user_id: str) -> Optional[Dict[str, Any]]:
    """按 user_id 取用户行。"""
    row = conn.execute(
        "SELECT * FROM sys_user WHERE user_id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def get_user_by_username(
    conn: sqlite3.Connection, username: str
) -> Optional[Dict[str, Any]]:
    """按登录名取用户行（登录接口用；不存在返回 None）。"""
    row = conn.execute(
        "SELECT * FROM sys_user WHERE username = ?", (username,)
    ).fetchone()
    return dict(row) if row else None


def list_users(conn: sqlite3.Connection, role: Optional[str] = None) -> List[Dict[str, Any]]:
    """用户列表（seed/管理页用；role 传参可过滤角色）。"""
    if role:
        rows = conn.execute(
            "SELECT * FROM sys_user WHERE role = ? ORDER BY created_at",
            (role,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sys_user ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


# ================================================================= 知识库文档
def insert_doc(
    conn: sqlite3.Connection, doc_name: str, doc_type: str, version: str,
    effective_from: str, effective_to: str, *, source_path: str = "",
    chunk_count: int = 0, uploaded_by: str = "",
) -> Dict[str, Any]:
    """新增文档注册行（版本冲突时抛 sqlite3.IntegrityError，由服务层转 40903）。"""
    doc_id = new_id("doc")
    now = _now()
    try:
        conn.execute(
            "INSERT INTO kb_doc"
            " (doc_id, doc_name, doc_type, version, effective_from,"
            "  effective_to, status, source_path, chunk_count, vector_status,"
            "  uploaded_by, occurred_at, recorded_at)"
            " VALUES (?,?,?,?,?,?, 'active', ?,?, 'pending', ?, ?, ?)",
            (doc_id, doc_name, doc_type, version, effective_from,
             effective_to, source_path, int(chunk_count), uploaded_by, now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise DuplicateDocVersion(doc_name, version) from None
    return get_doc(conn, doc_id) or {}


class DuplicateDocVersion(Exception):
    """同文档同版本已存在（服务层转 40903）。"""

    def __init__(self, doc_name: str, version: str):
        self.doc_name = doc_name
        self.version = version
        super().__init__(f"{doc_name} {version} 已存在")


def get_doc(conn: sqlite3.Connection, doc_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM kb_doc WHERE doc_id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def list_docs(
    conn: sqlite3.Connection, doc_type: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """文档列表（可按类型/状态筛选，管理页用）。"""
    sql, params = "SELECT * FROM kb_doc", []
    conds = []
    if doc_type:
        conds.append("doc_type = ?"); params.append(doc_type)
    if status:
        conds.append("status = ?"); params.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY recorded_at DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_doc_status(conn: sqlite3.Connection, doc_id: str, status: str,
                   actor: str = "") -> bool:
    """上/下架（active↔archived），记录操作人与时刻。返回是否命中。"""
    if status == "archived":
        cur = conn.execute(
            "UPDATE kb_doc SET status='archived', archived_at=?, archived_by=?"
            " WHERE doc_id=? AND status='active'",
            (_now(), actor, doc_id),
        )
    else:
        cur = conn.execute(
            "UPDATE kb_doc SET status='active', archived_at=NULL, archived_by=NULL"
            " WHERE doc_id=? AND status='archived'",
            (doc_id,),
        )
    conn.commit()
    return cur.rowcount > 0


def set_doc_vector_status(conn: sqlite3.Connection, doc_id: str, ok: bool) -> None:
    """向量化完成后回写状态（pending → ok）。"""
    conn.execute(
        "UPDATE kb_doc SET vector_status=? WHERE doc_id=?",
        ("ok" if ok else "pending", doc_id),
    )
    conn.commit()


def insert_chunks(conn: sqlite3.Connection, doc_id: str,
                  chunks: Sequence[Dict[str, Any]]) -> int:
    """批量写入某文档的切片，返回数量。"""
    now = _now()
    conn.executemany(
        "INSERT INTO kb_chunk"
        " (chunk_id, doc_id, seq_no, content, clause, char_len,"
        "  occurred_at, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            (new_id("chk"), doc_id, int(c["seq_no"]), c["content"],
             c.get("heading"), int(c["char_len"]), now, now)
            for c in chunks
        ],
    )
    conn.commit()
    return len(chunks)


def list_chunks_as_of(
    conn: sqlite3.Connection, as_of: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """按业务时刻取「当时有效」的切片（版本失效第一道闸）。

    这是版本有效性的唯一权威判定，直接在 SQL 层完成：
        status='active' AND effective_from <= as_of <= effective_to

    - as_of 为空 → 退化为「当前全部在架文档」，与索引文件行为一致；
    - 已下架（archived）的文档整篇不出现，无论相似度多高；
    - 生效区间为空的旧数据按「永久有效」处理，避免升级后检索突然空白。

    返回结构与 chunks.json 同构，供检索层直接建 BM25，不依赖索引文件。
    """
    sql = (
        "SELECT c.*, d.doc_name, d.doc_type, d.version, d.effective_from,"
        " d.effective_to, d.doc_id AS _doc_id"
        " FROM kb_chunk c JOIN kb_doc d ON c.doc_id = d.doc_id"
        " WHERE d.status = 'active'"
    )
    params: List[Any] = []
    if as_of:
        day = str(as_of)[:10]
        sql += (" AND (d.effective_from IS NULL OR d.effective_from = ''"
                " OR d.effective_from <= ?)")
        params.append(day)
        sql += (" AND (d.effective_to IS NULL OR d.effective_to = ''"
                " OR d.effective_to >= ?)")
        params.append(day)
    sql += " ORDER BY d.doc_name, d.version, c.seq_no"

    out: List[Dict[str, Any]] = []
    for r in conn.execute(sql, params).fetchall():
        out.append({
            "chunk_id": r["chunk_id"],
            "seq_no": r["seq_no"],
            "source_file": r["doc_name"],
            "heading": r["clause"] or "",
            "content": r["content"],
            "char_len": r["char_len"],
            "doc_meta": {
                "doc_id": r["_doc_id"],
                "doc_type": r["doc_type"],
                "version": r["version"],
                "effective_from": r["effective_from"],
                "effective_to": r["effective_to"],
                "status": "active",
            },
        })
    return out


def list_active_chunks_for_index(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """读全部 active 文档的切片，组装成检索索引所需的 chunks 结构。

    输出与 data/phase1_index/chunks.json 同构（含 source_file 与 doc_meta），
    供 rebuild_index 直接落盘——DB 为权威，索引文件为派生物。
    """
    rows = conn.execute(
        "SELECT c.*, d.doc_name, d.doc_type, d.version, d.effective_from,"
        " d.effective_to, d.status AS doc_status, d.doc_id AS _doc_id"
        " FROM kb_chunk c JOIN kb_doc d ON c.doc_id = d.doc_id"
        " WHERE d.status = 'active' ORDER BY d.recorded_at, c.doc_id, c.seq_no"
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "seq_no": r["seq_no"],
            "source_file": r["doc_name"],
            "heading": r["clause"] or "",
            "content": r["content"],
            "char_len": r["char_len"],
            "doc_meta": {
                "doc_id": r["_doc_id"],
                "doc_type": r["doc_type"],
                "version": r["version"],
                "effective_from": r["effective_from"],
                "effective_to": r["effective_to"],
                "status": r["doc_status"],
            },
        })
    return out


# ================================================================= 提示词模板
def get_active_template(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """取当前生效的系统提示词模板（全库仅一条 active）。"""
    row = conn.execute(
        "SELECT * FROM prompt_template WHERE status='active'"
        " ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def upsert_template(conn: sqlite3.Connection, name: str, content: str,
                    updated_by: str) -> Dict[str, Any]:
    """新建或更新 active 模板；每次修改追加一条历史（修改人+时间可审计）。

    没有任何模板时插入；已有 active 模板则 UPDATE 正文并写历史。
    """
    now = _now()
    current = get_active_template(conn)
    if current is None:
        template_id = new_id("pt")
        conn.execute(
            "INSERT INTO prompt_template"
            " (template_id, name, content, status, updated_by, updated_at)"
            " VALUES (?,?,?, 'active', ?, ?)",
            (template_id, name, content, updated_by, now),
        )
        conn.execute(
            "INSERT INTO prompt_template_history"
            " (history_id, template_id, content, updated_by, updated_at)"
            " VALUES (?,?,?,?,?)",
            (new_id("pth"), template_id, content, updated_by, now),
        )
        conn.commit()
        return get_active_template(conn) or {}
    conn.execute(
        "UPDATE prompt_template SET content=?, name=?, updated_by=?, updated_at=?"
        " WHERE template_id=?",
        (content, name, updated_by, now, current["template_id"]),
    )
    conn.execute(
        "INSERT INTO prompt_template_history"
        " (history_id, template_id, content, updated_by, updated_at)"
        " VALUES (?,?,?,?,?)",
        (new_id("pth"), current["template_id"], content, updated_by, now),
    )
    conn.commit()
    return get_active_template(conn) or {}


def list_template_history(conn: sqlite3.Connection, limit: int = 20) -> List[Dict[str, Any]]:
    """模板修改历史（管理页下方展示）。

    排序键 = updated_at DESC + rowid DESC：同一秒内的多次修改
    也要保持「最新在前」（秒级时间戳本身分不出先后，rowid 是落库顺序）。
    """
    rows = conn.execute(
        "SELECT h.*, t.name FROM prompt_template_history h"
        " JOIN prompt_template t ON h.template_id = t.template_id"
        " ORDER BY h.updated_at DESC, h.rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ================================================================= 引用
def save_citations(
    conn: sqlite3.Connection, message_id: str, citations: Sequence[Dict[str, Any]]
) -> None:
    """批量写入引用（结构化，便于审计与后续版本校验）。"""
    now = _now()
    conn.executemany(
        "INSERT INTO chat_message_citation"
        " (citation_id, message_id, seq, source_file, doc_id, clause,"
        "  version, doc_type, chunk_id, score, snippet, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                new_id("cit"), message_id, int(c.get("seq", i + 1)),
                c.get("source_file", ""), c.get("doc_id"), c.get("clause"),
                c.get("version"), c.get("doc_type"), c.get("chunk_id"),
                c.get("score"), c.get("snippet"), now,
            )
            for i, c in enumerate(citations)
        ],
    )
    conn.commit()


def get_citations(
    conn: sqlite3.Connection, message_id: str
) -> List[Dict[str, Any]]:
    """取某条消息的引用列表（按 seq 排序）。"""
    rows = conn.execute(
        "SELECT * FROM chat_message_citation WHERE message_id = ? ORDER BY seq",
        (message_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ================================================================= 审计标记
def set_message_flags(
    conn: sqlite3.Connection, message_id: str,
    root_cause: bool, no_citation: bool, stale_version: bool,
) -> None:
    """写入三条越界标记（机器标记，主管人工核查后走 mark_reviewed）。"""
    conn.execute(
        "UPDATE chat_message SET flag_root_cause=?, flag_no_citation=?,"
        " flag_stale_version=? WHERE message_id = ?",
        (int(root_cause), int(no_citation), int(stale_version), message_id),
    )
    conn.commit()


def get_message(conn: sqlite3.Connection, message_id: str) -> Optional[Dict[str, Any]]:
    """按 id 取单条消息（审计详情 / 补扫用）。"""
    row = conn.execute(
        "SELECT * FROM chat_message WHERE message_id = ?", (message_id,)
    ).fetchone()
    return dict(row) if row else None


def list_assistant_messages(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """全部 assistant 消息（补扫越界标记用，按落库顺序）。"""
    rows = conn.execute(
        "SELECT * FROM chat_message WHERE role='assistant' ORDER BY recorded_at, rowid"
    ).fetchall()
    return [dict(r) for r in rows]


def list_audit_messages(
    conn: sqlite3.Connection, *,
    role: Optional[str] = None, batch_no: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    only_flagged: bool = False, limit: int = 200,
) -> List[Dict[str, Any]]:
    """审计列表：消息联会话（带批次/角色），可多条件筛选。

    只列 assistant 消息（审计对象是回答，不是提问）；
    only_flagged=True 时只看命中任一越界规则的行。
    """
    sql = ("SELECT m.*, s.user_id, s.role AS session_role, s.batch_no,"
           " s.title AS session_title FROM chat_message m"
           " JOIN chat_session s ON m.session_id = s.session_id"
           " WHERE m.role='assistant'")
    params: List[Any] = []
    if role:
        sql += " AND s.role = ?"; params.append(role)
    if batch_no:
        sql += " AND s.batch_no = ?"; params.append(batch_no)
    if date_from:
        sql += " AND m.occurred_at >= ?"; params.append(date_from)
    if date_to:
        sql += " AND m.occurred_at <= ?"; params.append(date_to + "T23:59:59")
    if only_flagged:
        sql += (" AND (m.flag_root_cause=1 OR m.flag_no_citation=1"
                " OR m.flag_stale_version=1)")
    sql += " ORDER BY m.occurred_at DESC, m.rowid DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def mark_reviewed(
    conn: sqlite3.Connection, message_id: str,
    reviewed_by: str, note: str = "",
) -> bool:
    """主管标记已核查（机器标记 → 人工核查闭环）。返回是否命中。"""
    cur = conn.execute(
        "UPDATE chat_message SET reviewed_by=?, reviewed_at=?, reviewed_note=?"
        " WHERE message_id = ?",
        (reviewed_by, _now(), note, message_id),
    )
    conn.commit()
    return cur.rowcount > 0


def archived_doc_versions(conn: sqlite3.Connection) -> set:
    """已废止文档的 (doc_name, version) 集合——引用失效检测的判定源。

    与检索侧的权威源一致：kb_doc.status，而非 chunk 上的任何标记。
    """
    rows = conn.execute(
        "SELECT doc_name, version FROM kb_doc WHERE status='archived'"
    ).fetchall()
    return {(r["doc_name"], r["version"]) for r in rows}


# ================================================================= 工单 / 缺陷
def get_work_order(
    conn: sqlite3.Connection, work_order_id: str
) -> Optional[Dict[str, Any]]:
    """按单号取工单（移动端失焦校验用）。"""
    row = conn.execute(
        "SELECT * FROM work_order WHERE work_order_id = ?", (work_order_id,)
    ).fetchone()
    return dict(row) if row else None


def insert_work_order(
    conn: sqlite3.Connection, work_order_id: str, product_name: str,
    line_id: str, planned_qty: int = 0, occurred_at: Optional[str] = None,
) -> Dict[str, Any]:
    """新建工单（幂等：主键已存在则直接返回既有行）。

    `occurred_at` 同 insert_defect：不良率的分母必须能和分子的时间对齐，
    否则"这个月的不良"会去和"今天的产量"相除。
    """
    row = conn.execute(
        "SELECT * FROM work_order WHERE work_order_id = ?", (work_order_id,)
    ).fetchone()
    if row:
        return dict(row)
    now = _now()
    conn.execute(
        "INSERT INTO work_order"
        " (work_order_id, product_name, line_id, status, planned_qty,"
        "  occurred_at, recorded_at) VALUES (?,?,?, 'open', ?, ?, ?)",
        (work_order_id, product_name, line_id, int(planned_qty), occurred_at or now, now),
    )
    conn.commit()
    return dict(conn.execute(
        "SELECT * FROM work_order WHERE work_order_id = ?", (work_order_id,)
    ).fetchone())


def insert_defect(
    conn: sqlite3.Connection, defect_no: str, work_order_id: str, line_id: str,
    description: str, *, op_code: str = "", qty: int = 1, severity: str = "一般",
    defect_code: Optional[str] = None, suggest_json: str = "",
    client_draft_id: Optional[str] = None, status: str = "pending",
    is_backfill: bool = False, created_by: str = "",
    occurred_at: Optional[str] = None,
) -> Dict[str, Any]:
    """新建缺陷单（幂等：主键或 client_draft_id 已存在则返回既有行）。

    双时间列：occurred_at 与 recorded_at 分别传；补录由调用方置 is_backfill。

    `occurred_at` 可显式传入（默认 now）。补录场景必须传历史时刻，
    否则"三个月前那批货"会被记成今天的不良——趋势图上就出现一个今天的尖峰，
    而真正的历史位置是空的。演示数据与单测也靠这个参数造跨日数据。
    """
    row = conn.execute(
        "SELECT * FROM defect_item WHERE defect_no = ?", (defect_no,)
    ).fetchone()
    if row is None and client_draft_id:
        row = conn.execute(
            "SELECT * FROM defect_item WHERE client_draft_id = ?", (client_draft_id,)
        ).fetchone()
    if row:
        return dict(row)
    now = _now()
    happened = occurred_at or now
    conn.execute(
        "INSERT INTO defect_item"
        " (defect_no, work_order_id, line_id, op_code, description, qty,"
        "  severity, defect_code, suggest_json, photo_path, client_draft_id,"
        "  status, is_backfill, occurred_at, recorded_at, created_by, confirmed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,? ,?,?,?,? ,?)",
        (defect_no, work_order_id, line_id, op_code, description, int(qty),
         severity, defect_code, suggest_json, None, client_draft_id,
         status, int(is_backfill), happened, now, created_by,
         now if status != "draft" else None),
    )
    conn.commit()
    return dict(conn.execute(
        "SELECT * FROM defect_item WHERE defect_no = ?", (defect_no,)
    ).fetchone())


def get_defect(conn: sqlite3.Connection, defect_no: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM defect_item WHERE defect_no = ?", (defect_no,)
    ).fetchone()
    return dict(row) if row else None


def get_defect_by_draft_id(
    conn: sqlite3.Connection, client_draft_id: str
) -> Optional[Dict[str, Any]]:
    """按离线暂存幂等键取单——补传重复提交时直接返回原单，绝不落第二条。"""
    row = conn.execute(
        "SELECT * FROM defect_item WHERE client_draft_id = ?", (client_draft_id,)
    ).fetchone()
    return dict(row) if row else None


def list_defects(
    conn: sqlite3.Connection, *, line_id: Optional[str] = None,
    status: Optional[str] = None, limit: int = 50,
) -> List[Dict[str, Any]]:
    """缺陷单列表（最近的在前）。

    line_id 非空时强制按产线过滤——班组长只能看本线，过滤在 SQL 层完成，
    不依赖前端传参或页面隐藏。
    """
    sql = "SELECT * FROM defect_item"
    conds, params = [], []
    if line_id:
        conds.append("line_id = ?"); params.append(line_id)
    if status:
        conds.append("status = ?"); params.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY recorded_at DESC, rowid DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def confirm_defect_code(
    conn: sqlite3.Connection, defect_no: str, defect_code: str,
    actor: str = "",
) -> Optional[Dict[str, Any]]:
    """人工确认缺陷码：draft → pending。

    只写分类字段（defect_code），不写任何合格性判定——判定权在质量工程师。
    返回 None 表示单据不存在或不是 draft（已确认过的不可改，避免审计歧义）。
    """
    cur = conn.execute(
        "UPDATE defect_item SET defect_code=?, status='pending', confirmed_at=?,"
        " created_by=COALESCE(NULLIF(created_by, ''), ?)"
        " WHERE defect_no=? AND status='draft'",
        (defect_code, _now(), actor, defect_no),
    )
    conn.commit()
    return get_defect(conn, defect_no) if cur.rowcount > 0 else None


def set_defect_photo(
    conn: sqlite3.Connection, defect_no: str, photo_path: str
) -> None:
    """回写照片存储路径（先落库建单，再存文件，避免坏图阻塞建单）。"""
    conn.execute(
        "UPDATE defect_item SET photo_path=? WHERE defect_no=?",
        (photo_path, defect_no),
    )
    conn.commit()


def count_defects(conn: sqlite3.Connection, line_id: Optional[str] = None) -> int:
    """缺陷单计数（移动端顶栏角标用）。"""
    if line_id:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM defect_item WHERE line_id=?", (line_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS c FROM defect_item").fetchone()
    return int(row["c"])


# ================================================================ 缺陷码字典
def upsert_defect_code(
    conn: sqlite3.Connection, code: str, name: str, category: str = "其他",
    keywords: Optional[List[str]] = None, sort_no: int = 0,
) -> Dict[str, Any]:
    """新增/更新缺陷码（幂等：code 为主键，重复执行只更新描述字段）。"""
    payload = json.dumps(keywords or [], ensure_ascii=False)
    conn.execute(
        "INSERT INTO defect_code (code, name, category, keywords_json, sort_no)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
        " category=excluded.category, keywords_json=excluded.keywords_json,"
        " sort_no=excluded.sort_no",
        (code, name, category, payload, int(sort_no)),
    )
    conn.commit()
    return get_defect_code(conn, code) or {}


def get_defect_code(
    conn: sqlite3.Connection, code: str
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM defect_code WHERE code = ?", (code,)
    ).fetchone()
    return dict(row) if row else None


def list_defect_codes(
    conn: sqlite3.Connection, status: str = "active"
) -> List[Dict[str, Any]]:
    """启用中的缺陷码，按分组与排序号返回（移动端分组按钮的数据源）。"""
    rows = conn.execute(
        "SELECT * FROM defect_code WHERE status = ?"
        " ORDER BY category, sort_no, code",
        (status,),
    ).fetchall()
    return [dict(r) for r in rows]


def new_defect_no(conn: sqlite3.Connection) -> str:
    """生成缺陷编号 DF-YYYYMMDD-NNNN（当日流水，进程内加锁由调用方保证）。"""
    day = _now()[:10].replace("-", "")
    prefix = f"DF-{day}-"
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM defect_item WHERE defect_no LIKE ?",
        (prefix + "%",),
    ).fetchone()
    return f"{prefix}{int(row['c']) + 1:04d}"


# ================================================================= 归因单
def insert_attribution(
    conn: sqlite3.Connection, defect_no: str, evidence_json: str,
    hypothesis_text: str, *, submitted_by: str = "",
    draft_8d_md: str = "", attribution_id: str = "",
) -> Dict[str, Any]:
    """新建归因单（状态 submitted；幂等：显式传 id 且已存在时返回既有行）。"""
    if attribution_id:
        existing = get_attribution(conn, attribution_id)
        if existing:
            return existing
    aid = attribution_id or new_id("attr")
    conn.execute(
        "INSERT INTO attribution_order"
        " (attribution_id, defect_no, evidence_json, hypothesis_text,"
        "  status, draft_8d_md, submitted_by, submitted_at)"
        " VALUES (?,?,?,?, 'submitted', ?, ?, ?)",
        (aid, defect_no, evidence_json, hypothesis_text,
         draft_8d_md, submitted_by, _now()),
    )
    conn.commit()
    return get_attribution(conn, aid) or {}


def get_attribution(
    conn: sqlite3.Connection, attribution_id: str
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM attribution_order WHERE attribution_id = ?",
        (attribution_id,),
    ).fetchone()
    return dict(row) if row else None


def list_attributions(
    conn: sqlite3.Connection, status: Optional[str] = None,
    submitted_by: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """归因单列表；submitted_by 用于质量工程师「只看自己提交」的数据级过滤。"""
    sql, params = "SELECT * FROM attribution_order", []
    conds = []
    if status:
        conds.append("status = ?"); params.append(status)
    if submitted_by:
        conds.append("submitted_by = ?"); params.append(submitted_by)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY submitted_at DESC, rowid DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def decide_attribution(
    conn: sqlite3.Connection, attribution_id: str,
    decision: str, decided_by: str, note: str = "",
) -> Dict[str, Any]:
    """审批动作（状态机：仅 submitted 可批/退，否则由服务层转 40906）。

    返回 {ok: 是否成功, attribution: 最新行}；ok=False 表示当前状态不允许。
    """
    cur = conn.execute(
        "UPDATE attribution_order SET status=?, decided_by=?, decided_at=?,"
        " decision_note=? WHERE attribution_id=? AND status='submitted'",
        (decision, decided_by, _now(), note, attribution_id),
    )
    conn.commit()
    return {"ok": cur.rowcount > 0,
            "attribution": get_attribution(conn, attribution_id) or {}}


def set_attribution_draft(
    conn: sqlite3.Connection, attribution_id: str, draft_md: str
) -> None:
    """缓存 LLM 生成的 8D 草稿（下次导出直接读库，不重复调用）。"""
    conn.execute(
        "UPDATE attribution_order SET draft_8d_md=? WHERE attribution_id=?",
        (draft_md, attribution_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 分析取数（阶段 4 新增）
#
# 这一组函数与前面的"业务读写"有意分开：它们**只读**、不做业务判断、
# 返回原始行，聚合计算放在 core/analytics 里做。分开的理由很实际：
#   1. 统计口径要能被审计——SQL 里一个 GROUP BY 写错，没人看得出，
#      而 Python 里的分组逻辑可以写单测；
#   2. 同一批数据要被帕累托、控制图、趋势三个分析共用，
#      如果各自写 SQL，三处的过滤条件迟早会不一致（一个按 recorded_at、
#      另一个按 occurred_at，结论自然对不上）。
# ---------------------------------------------------------------------------
def query_defects(
    conn: sqlite3.Connection,
    *,
    line_id: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    op_code: Optional[str] = None,
    defect_code: Optional[str] = None,
    col: str = "occurred_at",
    limit: int = 20000,
) -> List[Dict[str, Any]]:
    """按条件取缺陷单原始行（分析用）。

    两个容易踩的点：

    - `col` 决定按哪个时间过滤。分析默认用 **occurred_at（业务时间）**，
      因为"这批货是什么时候出的问题"要用发生时刻；而"这个月录了多少单"
      要用 recorded_at。同一个报表里两者不能混，否则补录会让历史数据"倒流"——
      三个月前那次停产的不良，会在今天被录进来，按 recorded_at 统计就成了"今天的不良"。
    - `date_to` 自动补到当天 23:59:59。字符串比较下 "2026-11-30T09:00" > "2026-11-30"，
      不补就会出现"筛到 11 月 30 日却查不到 30 日当天数据"的经典 bug。
    """
    if col not in ("occurred_at", "recorded_at"):
        raise ValueError("时间列只能是 occurred_at 或 recorded_at")

    sql = "SELECT * FROM defect_item WHERE 1=1"
    params: List[Any] = []
    if line_id:
        sql += " AND line_id = ?"
        params.append(line_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if op_code:
        sql += " AND op_code = ?"
        params.append(op_code)
    if defect_code:
        sql += " AND defect_code = ?"
        params.append(defect_code)
    if date_from:
        sql += " AND %s >= ?" % col
        params.append(str(date_from)[:10])
    if date_to:
        sql += " AND %s <= ?" % col
        params.append(_end_of_day(date_to))
    sql += " ORDER BY %s ASC, rowid ASC LIMIT ?" % col
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _end_of_day(value: Any) -> str:
    """把 'YYYY-MM-DD' 补成 'YYYY-MM-DD~'。

    用 `~` 而不是 `T23:59:59`：ASCII 里 `~`(0x7E) 大于任何数字与字母，
    因此任何以该日期开头的字符串都 ≤ 'YYYY-MM-DD~'，包括 "2026-11-30"、
    "2026-11-30T23:59:59"、"2026-11-30 08:00"。用 T23:59:59 反而会漏掉
    "2026-11-30T23:59:59.500" 这种带毫秒的写法。
    """
    s = str(value)
    return s if len(s) > 10 else s + "~"


def query_work_orders(
    conn: sqlite3.Connection,
    *,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    col: str = "occurred_at",
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    """取工单（不良率的分母）。"""
    if col not in ("occurred_at", "recorded_at"):
        raise ValueError("时间列只能是 occurred_at 或 recorded_at")
    sql = "SELECT * FROM work_order WHERE 1=1"
    params: List[Any] = []
    if line_id:
        sql += " AND line_id = ?"
        params.append(line_id)
    if date_from:
        sql += " AND %s >= ?" % col
        params.append(str(date_from)[:10])
    if date_to:
        sql += " AND %s <= ?" % col
        params.append(_end_of_day(date_to))
    sql += " ORDER BY %s ASC LIMIT ?" % col
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def quality_kpi(
    conn: sqlite3.Connection,
    *,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    col: str = "occurred_at",
) -> Dict[str, Any]:
    """一次 SQL 拿到 KPI 需要的基础计数（避免 5 次全表扫描）。

    `SUM(CASE WHEN ...)` 的写法比多次查询快得多，但要注意 `qty` 可能是 NULL，
    所以用 `COALESCE(qty, 0)`；`COUNT(*)` 与 `SUM(qty)` 的区别是
    "几单"与"几件"——报告里这两个数经常被混用，导致"不良率 200%"的笑话。
    """
    where = " WHERE 1=1"
    params: List[Any] = []
    if line_id:
        where += " AND line_id = ?"
        params.append(line_id)
    if date_from:
        where += " AND %s >= ?" % col
        params.append(str(date_from)[:10])
    if date_to:
        where += " AND %s <= ?" % col
        params.append(_end_of_day(date_to))

    row = conn.execute(
        "SELECT COUNT(*) AS orders,"
        " COALESCE(SUM(COALESCE(qty,0)),0) AS pieces,"
        " COALESCE(SUM(CASE WHEN status='draft' THEN 1 ELSE 0 END),0) AS drafts,"
        " COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),0) AS pendings,"
        " COALESCE(SUM(COALESCE(is_backfill,0)),0) AS backfills,"
        " COALESCE(SUM(CASE WHEN severity='严重' THEN 1 ELSE 0 END),0) AS severe,"
        " COALESCE(SUM(CASE WHEN defect_code IS NULL OR defect_code='' THEN 1 ELSE 0 END),0) AS uncoded"
        " FROM defect_item" + where,
        params,
    ).fetchone()

    prod = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(planned_qty,0)),0) AS planned FROM work_order" + where,
        params,
    ).fetchone()

    kpi = dict(row) if row else {}
    kpi["planned_qty"] = prod["planned"] if prod else 0
    return kpi


def distinct_lines(conn: sqlite3.Connection) -> List[str]:
    """所有出现过的产线（看板的产线下拉框用）。"""
    rows = conn.execute(
        "SELECT DISTINCT line_id FROM defect_item WHERE line_id IS NOT NULL"
        " UNION SELECT DISTINCT line_id FROM work_order WHERE line_id IS NOT NULL"
        " ORDER BY 1"
    ).fetchall()
    return [r["line_id"] for r in rows]


# ---------------------------------------------------------------------------
# 计量型实测值（阶段 4：SPC 的数据源）
# ---------------------------------------------------------------------------
def insert_measurement(
    conn: sqlite3.Connection,
    item_code: str,
    value: float,
    line_id: str,
    *,
    work_order_id: Optional[str] = None,
    op_code: Optional[str] = None,
    subgroup_no: int = 1,
    unit: str = "mm",
    usl: Optional[float] = None,
    lsl: Optional[float] = None,
    gauge: Optional[str] = None,
    operator: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> Dict[str, Any]:
    """写一条实测值。

    `subgroup_no` 由调用方给定而不是自增：子组是"同一次抽检的 5 件"，
    这个分组信息只有现场知道（几点几分抽的、哪一模的），系统不能替它猜。
    如果让它自增，同一小时的两批抽检会被合并成一个 10 件的子组，
    σ̂ 立刻失真——控制限跟着全错。
    """
    record_id = new_id("MR")
    ts = occurred_at or _now()
    conn.execute(
        "INSERT INTO meas_record (record_id, item_code, work_order_id, line_id, op_code,"
        " subgroup_no, value, unit, usl, lsl, gauge, operator, occurred_at, recorded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            record_id, item_code, work_order_id, line_id, op_code,
            int(subgroup_no), float(value), unit, usl, lsl, gauge, operator,
            ts, _now(),
        ),
    )
    conn.commit()
    return get_measurement(conn, record_id) or {}


def insert_measurements(conn: sqlite3.Connection, rows: Sequence[Dict[str, Any]]) -> int:
    """批量写入（导入 Excel 时的路径）。返回写入条数。

    不做 `executemany` 之外的花哨优化，但**整体一个事务**：
    导入 5000 行时逐条 commit 会慢 30 倍以上，而且中途失败会留下半批数据——
    半批数据比没有数据更危险，因为看板上看不出它是半批。
    """
    if not rows:
        return 0
    payload = []
    now = _now()
    for r in rows:
        payload.append(
            (
                new_id("MR"),
                r.get("item_code") or "未命名",
                r.get("work_order_id"),
                r.get("line_id") or "UNKNOWN",
                r.get("op_code"),
                int(r.get("subgroup_no") or 1),
                float(r.get("value") or 0),
                r.get("unit") or "mm",
                r.get("usl"),
                r.get("lsl"),
                r.get("gauge"),
                r.get("operator"),
                r.get("occurred_at") or now,
                now,
            )
        )
    conn.executemany(
        "INSERT INTO meas_record (record_id, item_code, work_order_id, line_id, op_code,"
        " subgroup_no, value, unit, usl, lsl, gauge, operator, occurred_at, recorded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        payload,
    )
    conn.commit()
    return len(payload)


def get_measurement(conn: sqlite3.Connection, record_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM meas_record WHERE record_id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def query_measurements(
    conn: sqlite3.Connection,
    *,
    item_code: Optional[str] = None,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50000,
) -> List[Dict[str, Any]]:
    """按项目/产线/时间取实测值，按子组顺序返回。

    排序用 `subgroup_no` 而不是时间：同一次抽检的 5 件可能跨秒写入，
    按时间排会把子组打散；子组顺序错了，X̄-R 图上的点就全乱了。
    """
    sql = "SELECT * FROM meas_record WHERE 1=1"
    params: List[Any] = []
    if item_code:
        sql += " AND item_code = ?"
        params.append(item_code)
    if line_id:
        sql += " AND line_id = ?"
        params.append(line_id)
    if date_from:
        sql += " AND occurred_at >= ?"
        params.append(str(date_from)[:10])
    if date_to:
        sql += " AND occurred_at <= ?"
        params.append(_end_of_day(date_to))
    sql += " ORDER BY occurred_at ASC, subgroup_no ASC, rowid ASC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_measurement_items(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """有哪些检验项目、各多少条、规格限是什么（看板的下拉框与默认规格）。

    排序按**记录数降序**，不是按名称：下拉框的第一个选项就是默认选中项，
    按名称排的话很可能落到一个只有几条读数的冷门项目上，
    用户点开看板/一致性检查第一眼就是"数据不足"——他会以为系统坏了。
    数据多的项目先出现，既是合理默认值，也让演示与日常使用都顺手。
    """
    rows = conn.execute(
        "SELECT item_code, line_id, unit, COUNT(*) AS n,"
        " AVG(value) AS mean_value, MIN(value) AS min_value, MAX(value) AS max_value,"
        " MAX(usl) AS usl, MIN(lsl) AS lsl"
        " FROM meas_record GROUP BY item_code, line_id, unit"
        " ORDER BY n DESC, item_code, line_id"
    ).fetchall()
    return [dict(r) for r in rows]


def count_measurements(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM meas_record").fetchone()
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# 批次谱系（阶段 5）
# ---------------------------------------------------------------------------
def insert_material_lot(
    conn: sqlite3.Connection,
    lot_id: str,
    material_code: str,
    material_name: str,
    *,
    supplier: Optional[str] = None,
    supplier_lot: Optional[str] = None,
    qty: float = 0.0,
    unit: str = "kg",
    status: str = "in_use",
    occurred_at: Optional[str] = None,
) -> Dict[str, Any]:
    """登记一个物料批（幂等：批号已存在直接返回）。

    批号由业务给定而不是自增：外来物料的批号要能对上供应商的送货单，
    自增号在跟供应商扯皮时毫无意义——对方只认他们自己的批号。
    内部批号 `lot_id` 与 `supplier_lot` 分开存，正是为了"对内唯一、对外可对"。
    """
    row = conn.execute("SELECT * FROM material_lot WHERE lot_id=?", (lot_id,)).fetchone()
    if row:
        return dict(row)
    now = _now()
    conn.execute(
        "INSERT INTO material_lot (lot_id, material_code, material_name, supplier,"
        " supplier_lot, qty, unit, status, occurred_at, recorded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (lot_id, material_code, material_name, supplier, supplier_lot,
         float(qty), unit, status, occurred_at or now, now),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM material_lot WHERE lot_id=?", (lot_id,)).fetchone())


def get_material_lot(conn: sqlite3.Connection, lot_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM material_lot WHERE lot_id=?", (lot_id,)).fetchone()
    return dict(row) if row else None


def list_material_lots(
    conn: sqlite3.Connection,
    *,
    supplier: Optional[str] = None,
    material_code: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """物料批列表（按下发时间倒序）。"""
    sql = "SELECT * FROM material_lot WHERE 1=1"
    params: List[Any] = []
    for col, val in (("supplier", supplier), ("material_code", material_code), ("status", status)):
        if val:
            sql += " AND %s = ?" % col
            params.append(val)
    sql += " ORDER BY occurred_at DESC, rowid DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def link_lot_to_work_order(
    conn: sqlite3.Connection,
    lot_id: str,
    work_order_id: str,
    *,
    qty_used: float = 0.0,
    op_code: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> Dict[str, Any]:
    """记一笔"这批料用在了这张工单"。

    唯一索引 (lot_id, work_order_id) 保证重复上报不会凭空放大影响面——
    现场扫码枪抖一下连发两次是常态，如果每次都插一条，
    一批料的"影响工单数"会翻倍，召回范围跟着翻倍，代价是真金白银。
    """
    now = _now()
    ts = occurred_at or now
    try:
        conn.execute(
            "INSERT INTO lot_consumption (lot_id, work_order_id, qty_used, op_code,"
            " occurred_at, recorded_at) VALUES (?,?,?,?,?,?)",
            (lot_id, work_order_id, float(qty_used), op_code, ts, now),
        )
        conn.commit()
        inserted = True
    except sqlite3.IntegrityError:
        # 已存在：只补数量（可能第一次报的是部分投入），不改时间
        conn.execute(
            "UPDATE lot_consumption SET qty_used = qty_used + ?"
            " WHERE lot_id=? AND work_order_id=?",
            (float(qty_used), lot_id, work_order_id),
        )
        conn.commit()
        inserted = False
    row = conn.execute(
        "SELECT * FROM lot_consumption WHERE lot_id=? AND work_order_id=?",
        (lot_id, work_order_id),
    ).fetchone()
    out = dict(row) if row else {}
    out["inserted"] = inserted
    return out


def list_work_orders_of_lot(conn: sqlite3.Connection, lot_id: str) -> List[Dict[str, Any]]:
    """正查第一步：这批料进了哪些工单（带工单信息与用料量）。"""
    rows = conn.execute(
        "SELECT c.*, w.product_name, w.line_id, w.planned_qty, w.occurred_at AS wo_time"
        " FROM lot_consumption c LEFT JOIN work_order w ON c.work_order_id = w.work_order_id"
        " WHERE c.lot_id = ? ORDER BY c.occurred_at ASC",
        (lot_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_lots_of_work_order(conn: sqlite3.Connection, work_order_id: str) -> List[Dict[str, Any]]:
    """反查第一步：这张工单吃了哪些料（带批次与供应商信息）。"""
    rows = conn.execute(
        "SELECT c.qty_used, c.op_code, c.occurred_at AS used_at,"
        " l.lot_id, l.material_code, l.material_name, l.supplier, l.supplier_lot,"
        " l.qty AS lot_qty, l.unit, l.status, l.occurred_at AS received_at"
        " FROM lot_consumption c JOIN material_lot l ON c.lot_id = l.lot_id"
        " WHERE c.work_order_id = ? ORDER BY c.occurred_at ASC",
        (work_order_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_suppliers(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT supplier FROM material_lot WHERE supplier IS NOT NULL"
        " AND supplier <> '' ORDER BY 1"
    ).fetchall()
    return [r["supplier"] for r in rows]


def count_lots(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM material_lot").fetchone()
    return int(row["n"]) if row else 0


def suppliers_of_defect(conn: sqlite3.Connection, defect_no: str) -> List[str]:
    """一条不良单涉及哪些供应商（反查的快捷版，给看板打标用）。"""
    rows = conn.execute(
        "SELECT DISTINCT l.supplier FROM defect_item d"
        " JOIN lot_consumption c ON c.work_order_id = d.work_order_id"
        " JOIN material_lot l ON l.lot_id = c.lot_id"
        " WHERE d.defect_no = ? AND l.supplier IS NOT NULL",
        (defect_no,),
    ).fetchall()
    return [r["supplier"] for r in rows]


# ================================================================= MSA 研究
def insert_msa_study(
    conn: sqlite3.Connection, study_id: str, item_code: str,
    design_json: str, raw_json: str, result_json: str,
    *, gauge: Optional[str] = None, line_id: Optional[str] = None,
    tolerance: Optional[float] = None, verdict: str = "unknown",
    pct_grr: Optional[float] = None, ndc: Optional[float] = None,
    source: str = "manual", created_by: str = "",
    occurred_at: Optional[str] = None,
) -> Dict[str, Any]:
    """写入一次 MSA 研究（幂等：同 study_id 覆盖更新）。"""
    now = _now()
    conn.execute(
        "INSERT INTO msa_study (study_id, item_code, gauge, line_id, tolerance,"
        " design_json, raw_json, result_json, verdict, pct_grr, ndc, source,"
        " created_by, occurred_at, recorded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(study_id) DO UPDATE SET result_json=excluded.result_json,"
        " verdict=excluded.verdict, pct_grr=excluded.pct_grr, ndc=excluded.ndc",
        (study_id, item_code, gauge, line_id, tolerance, design_json, raw_json,
         result_json, verdict, pct_grr, ndc, source, created_by,
         occurred_at or now, now),
    )
    conn.commit()
    return get_msa_study(conn, study_id) or {}


def get_msa_study(conn: sqlite3.Connection, study_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM msa_study WHERE study_id = ?", (study_id,)
    ).fetchone()
    return dict(row) if row else None


def list_msa_studies(
    conn: sqlite3.Connection, *, item_code: Optional[str] = None,
    verdict: Optional[str] = None, limit: int = 50,
) -> List[Dict[str, Any]]:
    """研究记录列表（不含 raw_json，列表页不需要几万行读数）。"""
    sql = ("SELECT study_id, item_code, gauge, line_id, tolerance, verdict,"
           " pct_grr, ndc, source, created_by, occurred_at, design_json"
           " FROM msa_study WHERE 1=1")
    args: List[Any] = []
    if item_code:
        sql += " AND item_code = ?"
        args.append(item_code)
    if verdict:
        sql += " AND verdict = ?"
        args.append(verdict)
    sql += " ORDER BY occurred_at DESC, rowid DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def latest_msa_of_item(conn: sqlite3.Connection, item_code: str) -> Optional[Dict[str, Any]]:
    """某检验项目最近一次 MSA（看板与"缺 MSA"清单的判断依据）。"""
    row = conn.execute(
        "SELECT * FROM msa_study WHERE item_code = ?"
        " ORDER BY occurred_at DESC, rowid DESC LIMIT 1",
        (item_code,),
    ).fetchone()
    return dict(row) if row else None


def count_msa_studies(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM msa_study").fetchone()
    return int(row["n"]) if row else 0


def items_without_msa(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    """有实测值但从未做过 MSA 的检验项目。

    这是本项目里最值得被看见的一张表：它把"我们一直在画控制图，
    但从没验证过尺子"这件事变成了一个可以点开的名字。
    """
    rows = conn.execute(
        "SELECT m.item_code, m.line_id, COUNT(*) AS records,"
        "       MIN(m.occurred_at) AS first_at, MAX(m.occurred_at) AS last_at"
        " FROM meas_record m"
        " WHERE NOT EXISTS (SELECT 1 FROM msa_study s WHERE s.item_code = m.item_code)"
        " GROUP BY m.item_code, m.line_id"
        " ORDER BY records DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]
