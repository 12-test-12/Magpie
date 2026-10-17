-- Human research work records. These do not duplicate assets or evidence and
-- are not parser output. Import Undo/Redo does not erase researcher decisions.
BEGIN IMMEDIATE;
CREATE TABLE research_tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 300),
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','in_progress','done','shelved')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    UNIQUE(id, project_id)
);
CREATE INDEX ix_research_tasks_project ON research_tasks(project_id, status, id);

CREATE TABLE research_task_links (
    project_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    PRIMARY KEY(project_id, task_id, entity_id),
    FOREIGN KEY(task_id, project_id) REFERENCES research_tasks(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY(entity_id, project_id) REFERENCES entities(id, project_id)
);
CREATE INDEX ix_research_links_entity ON research_task_links(entity_id);

-- Tags continue to use entity_tags and ManualWriter. Tier is human priority,
-- independent of observed severity or the risk score. NULL = unclassified.
CREATE TABLE entity_research (
    project_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    criticality INTEGER CHECK(criticality IN (1,2,3)),
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY(project_id, entity_id),
    FOREIGN KEY(entity_id, project_id) REFERENCES entities(id, project_id)
);
CREATE INDEX ix_entity_research_tier ON entity_research(project_id, criticality, entity_id);
