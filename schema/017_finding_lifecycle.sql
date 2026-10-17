-- =============================================================================
--  017_finding_lifecycle.sql —— 发现生命周期、人工处置与复测记录（工作包 F）
--
--  设计原则：
--    * 工具命中不等于已确认漏洞：未处置时默认为 candidate（待核实）。
--    * 发现的真实性判断与处置状态分离：支持 candidate / confirmed / false_positive / duplicate / inconclusive / resolved。
--    * 人工复测必须有依据：每条复测记录关联 status、notes、actor、时间与可选 evidence_id。
--    * 超时、凭据失效或网络不可达不自动判定为 resolved。
--    * 事实层继续沿用已有的 vuln 实体与 found_on 关系，不另建第二套资产/漏洞事实库。
-- =============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE finding_triage (
    project_id         INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    finding_id         INTEGER NOT NULL,
    status             TEXT    NOT NULL DEFAULT 'candidate'
                       CHECK (status IN ('candidate','confirmed','false_positive','duplicate','inconclusive','resolved')),
    severity           TEXT
                       CHECK (severity IS NULL OR severity IN ('critical','high','medium','low','info')),
    title              TEXT,
    disposition_reason TEXT,
    duplicate_of_id    INTEGER,
    updated_at         TEXT    NOT NULL,
    updated_by         TEXT    NOT NULL DEFAULT 'analyst',
    PRIMARY KEY (project_id, finding_id),
    FOREIGN KEY (finding_id, project_id) REFERENCES entities(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (duplicate_of_id, project_id) REFERENCES entities(id, project_id) ON DELETE SET NULL
);

CREATE INDEX ix_finding_triage_status ON finding_triage(project_id, status);

CREATE TABLE finding_retests (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    finding_id         INTEGER NOT NULL,
    status             TEXT    NOT NULL
                       CHECK (status IN ('still_vulnerable','resolved','inconclusive')),
    notes              TEXT    NOT NULL,
    evidence_id        INTEGER,
    actor              TEXT    NOT NULL DEFAULT 'analyst',
    created_at         TEXT    NOT NULL,
    FOREIGN KEY (finding_id, project_id) REFERENCES entities(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (evidence_id, project_id) REFERENCES evidence(id, project_id) ON DELETE SET NULL
);

CREATE INDEX ix_finding_retests_finding ON finding_retests(project_id, finding_id, id);
