-- Human revisions and references only; original facts and evidence are immutable.
BEGIN IMMEDIATE;
CREATE TABLE note_revisions (
    project_id INTEGER NOT NULL,
    observation_id INTEGER NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    text TEXT NOT NULL CHECK(length(trim(text)) BETWEEN 1 AND 10000),
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 2000),
    evidence_ids TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_ids)),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    PRIMARY KEY(project_id, observation_id, version),
    FOREIGN KEY(observation_id, project_id) REFERENCES observations(id, project_id)
);
CREATE TABLE note_evidence_links (
    project_id INTEGER NOT NULL,
    observation_id INTEGER NOT NULL,
    evidence_id INTEGER NOT NULL,
    PRIMARY KEY(project_id, observation_id, evidence_id),
    FOREIGN KEY(observation_id, project_id) REFERENCES observations(id, project_id),
    FOREIGN KEY(evidence_id, project_id) REFERENCES evidence(id, project_id)
);
CREATE TABLE research_task_evidence (
    project_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    evidence_id INTEGER NOT NULL,
    PRIMARY KEY(project_id, task_id, evidence_id),
    FOREIGN KEY(task_id, project_id) REFERENCES research_tasks(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY(evidence_id, project_id) REFERENCES evidence(id, project_id)
);
CREATE INDEX ix_note_evidence ON note_evidence_links(project_id,evidence_id);
CREATE INDEX ix_task_evidence ON research_task_evidence(project_id,evidence_id);
CREATE TABLE entity_manual_values (
    project_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    display_name TEXT,
    review_status TEXT CHECK(review_status IS NULL OR review_status IN ('unreviewed','reviewed','needs_review')),
    version INTEGER NOT NULL,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY(project_id,entity_id),
    FOREIGN KEY(entity_id,project_id) REFERENCES entities(id,project_id)
);
INSERT OR IGNORE INTO multivalue_fields VALUES ('observation_field','js_indicator','JS 中的脱敏 URL/域名和敏感串候选，不代表请求或确认泄露');
-- The migration runner commits schema and registry atomically.
UPDATE kind_registry SET produced_by=json_insert(produced_by,'$[#]','js-manifest@3')
 WHERE json_valid(produced_by) AND EXISTS(SELECT 1 FROM json_each(produced_by) WHERE value='js-manifest@2');
UPDATE kind_registry SET produced_by=json_insert(produced_by,'$[#]','har@4')
 WHERE json_valid(produced_by) AND (kind='flow' OR EXISTS(SELECT 1 FROM json_each(produced_by) WHERE value='har@3'));
