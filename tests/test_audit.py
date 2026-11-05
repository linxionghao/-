"""批次 1c 对话审计测试：三条越界规则 + 单条检测 + 全量补扫。

设计要点：
- `detect_flags` 是纯函数，测试无需数据库，覆盖全部正则分支与边界；
- `flag_message` / `rescan_all` 走临时库，验证「落库 + 不覆盖人工核查结论」；
- 规则二只对 complete 消息生效——partial 不算无引用作答，这是审计口径的关键。
"""

from __future__ import annotations

import pytest

from quality_trace.core.repo import db as repo
from quality_trace.core.services import audit


# ---------------------------------------------------------------------------
# 规则一：根因结论表述（系统只允许 L3「因果假设（待验证）」）
# ---------------------------------------------------------------------------
class TestRuleRootCause:
    """覆盖 5 组正则：已确定 / 就是 / 根本原因是 / 可以确认 / 无需验证。"""

    @pytest.mark.parametrize("text", [
        "根因已确定：切刀磨损。",              # 根因(已)?(确定|查明|找到|锁定)
        "根因查明为对刀误差。",
        "根因就是冷却液浓度偏低。",
        "根因是夹具定位面磨损。",
        "根本原因就是来料硬度超差。",
        "根本原因为刀具寿命到期。",
        "可以确认本次问题的根因在热处理工序。",
        "根因无需进一步验证。",
        "根本原因不必再做验证。",
    ])
    def test_hits_when_conclusive(self, text):
        """任何宣称根因已定的表述都必须命中规则一。"""
        flags = audit.detect_flags(text, "complete", [], set())
        assert flags["root_cause"] is True

    @pytest.mark.parametrize("text", [
        "疑似根因：切刀磨损（待验证）。",       # 合规表述：L3 假设
        "初步判断可能与刀具寿命相关，需进一步验证。",
        "以下为待验证假设，不构成根因结论。",
        "本次不讨论根因，仅说明围堵措施。",
    ])
    def test_misses_when_hypothesis(self, text):
        """「疑似 / 待验证」表述不得误报——误报会让审计失去可信度。"""
        flags = audit.detect_flags(text, "complete", [], set())
        assert flags["root_cause"] is False

    def test_empty_content_safe(self):
        """空正文不抛异常、不命中（LLM 返回空时走降级，不该再叠一条越界）。"""
        assert audit.detect_flags("", "complete", [], set())["root_cause"] is False
        assert audit.detect_flags(None, "complete", [], set())["root_cause"] is False


# ---------------------------------------------------------------------------
# 规则二：无引用作答
# ---------------------------------------------------------------------------
class TestRuleNoCitation:
    """回答完成且有正文，却一条引用都没有 → 疑似凭空作答。"""

    def test_flagged_when_complete_without_citation(self):
        flags = audit.detect_flags("按标准应全检。", "complete", [], set())
        assert flags["no_citation"] is True

    def test_not_flagged_when_has_citation(self):
        citations = [{"source_file": "检验规范", "version": "v2"}]
        flags = audit.detect_flags("按标准应全检〔C1〕。", "complete", citations, set())
        assert flags["no_citation"] is False

    def test_partial_not_flagged(self):
        """流式中途落库的 partial 消息不算越界——否则每次异常断流都产生噪声。"""
        flags = audit.detect_flags("按标准应", "partial", [], set())
        assert flags["no_citation"] is False

    def test_empty_answer_not_flagged(self):
        """正文为空的 complete（例如纯降级无文本）不该判为无引用作答。"""
        flags = audit.detect_flags("   ", "complete", [], set())
        assert flags["no_citation"] is False


# ---------------------------------------------------------------------------
# 规则三：引用了已废止版本
# ---------------------------------------------------------------------------
class TestRuleStaleVersion:
    """按（文档名 + 版本号）联合判定，同文档其他版本不受牵连。"""

    def test_flagged_when_citation_archived(self):
        archived = {("螺纹外径检验规范", "v1")}
        citations = [{"source_file": "螺纹外径检验规范", "version": "v1"}]
        flags = audit.detect_flags("外径公差 0.02。", "complete", citations, archived)
        assert flags["stale_version"] is True

    def test_not_flagged_for_other_version(self):
        """v1 废止不影响 v2 的引用——版本是判定维度，不能按文档名一刀切。"""
        archived = {("螺纹外径检验规范", "v1")}
        citations = [{"source_file": "螺纹外径检验规范", "version": "v2"}]
        flags = audit.detect_flags("外径公差 0.02。", "complete", citations, archived)
        assert flags["stale_version"] is False

    def test_any_one_stale_triggers(self):
        """多条引用中只要有一条失效即命中，方便主管从最严重的往下看。"""
        archived = {("旧版作业指导书", "v3")}
        citations = [
            {"source_file": "螺纹外径检验规范", "version": "v2"},
            {"source_file": "旧版作业指导书", "version": "v3"},
        ]
        flags = audit.detect_flags("按作业指导书执行。", "complete", citations, archived)
        assert flags["stale_version"] is True

    def test_missing_fields_tolerated(self):
        """引用缺字段（历史脏数据）不应抛异常，也不该误判。"""
        citations = [{"source_file": None, "version": "v1"}, {"version": "v9"}]
        flags = audit.detect_flags("正文。", "complete", citations, set())
        assert flags["stale_version"] is False


# ---------------------------------------------------------------------------
# 落库与补扫：临时库验证「标记落库 + 不覆盖人工核查」
# ---------------------------------------------------------------------------
@pytest.fixture()
def conn(tmp_path):
    """独立临时库，避免污染演示数据。"""
    c = repo.connect(str(tmp_path / "audit.db"))
    repo.init_schema(c)
    yield c
    c.close()


def _assistant(conn, content: str, *, status: str = "complete", session=None):
    """写入一条 assistant 消息，返回 message_id。"""
    sess = session or repo.create_session(conn, "qe-001", "quality_engineer")
    return repo.save_message(conn, sess["session_id"], "assistant", content,
                             status=status)


class TestFlagMessage:
    def test_flags_persisted(self, conn):
        """flag_message 要把三列写进 chat_message，供管理页筛选。"""
        mid = _assistant(conn, "根因已确定：切刀磨损。")
        flags = audit.flag_message(conn, mid)
        assert flags == {"root_cause": True, "no_citation": True,
                         "stale_version": False}
        row = repo.get_message(conn, mid)
        assert row["flag_root_cause"] == 1 and row["flag_no_citation"] == 1

    def test_ignores_user_message(self, conn):
        """审计对象是回答：user 消息即使命中正则也不打标。"""
        sess = repo.create_session(conn, "qe-001", "quality_engineer")
        mid = repo.save_message(conn, sess["session_id"], "user", "根因已确定吗？")
        flags = audit.flag_message(conn, mid)
        assert not any(flags.values())
        assert repo.get_message(conn, mid)["flag_root_cause"] == 0

    def test_missing_message_safe(self, conn):
        """不存在的消息 id 返回全 False，不抛异常（补扫遇到脏 id 时兜底）。"""
        assert not any(audit.flag_message(conn, "msg-not-exist").values())


class TestRescan:
    def test_rescan_counts(self, conn):
        """补扫统计口径：scanned 为 assistant 总数，flagged 为命中任一规则的条数。"""
        archived_doc = repo.insert_doc(conn, "旧规范", "standard", "v1",
                                       "2025-01-01", "2025-12-31")
        repo.set_doc_status(conn, archived_doc["doc_id"], "archived", "tester")

        mid1 = _assistant(conn, "根因已确定。")
        mid2 = _assistant(conn, "按标准执行。")            # 无引用
        mid3 = _assistant(conn, "按〔C1〕执行。")
        repo.save_citations(conn, mid3, [{
            "seq": 1, "chunk_id": "c1", "source_file": "旧规范",
            "version": "v1", "section": "3.1", "snippet": "正文",
        }])

        result = audit.rescan_all(conn)
        assert result["scanned"] == 3
        assert result["flagged"] == 3
        assert repo.get_message(conn, mid3)["flag_stale_version"] == 1

    def test_rescan_keeps_reviewed(self, conn):
        """机器重扫不得抹掉人工核查结论——审计链的可信度靠这条。"""
        mid = _assistant(conn, "根因已确定。")
        audit.flag_message(conn, mid)
        repo.mark_reviewed(conn, mid, "manager-01", "已要求工程师补充验证")

        audit.rescan_all(conn)
        row = repo.get_message(conn, mid)
        assert row["reviewed_by"] == "manager-01"
        assert row["reviewed_note"] == "已要求工程师补充验证"
        assert row["flag_root_cause"] == 1
