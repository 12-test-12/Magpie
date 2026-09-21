-- =============================================================================
--  008_pipeline_definitions.sql —— 管线**定义**与管线**运行**（开发实施计划 3.7b）
--
--  与 004–007 同一条纪律：**只增不改**（红线 R4）。已应用的迁移被改动 →
--  校验和不匹配 → `migrate()` 直接报错。
--
--  ---------------------------------------------------------------------------
--  为什么这两张表不是"事实"
--  ---------------------------------------------------------------------------
--  本步引入一个新的持久化产物：**一条可复用的管线定义**。它必须落库，但它
--  **不是事实层**（v3.2 的 facts 是 observations / relations / entities）：
--
--    * 保存 / 改名 / 归档一个定义，**不建 job、不产生 contribution、不参与
--      undo / redo**（3.7a 冻结的 F4）。undo 的口径是"贡献的活跃性"，判据是
--      B 组的逐行指纹相等；把一个不产生贡献的东西塞进 job，会让"撤到哪"这个
--      问题同时有两个答案。
--    * 反过来同样成立：**跑一次管线不修改定义**。跑是读定义、写事实。
--      定义的行数 / revision / updated_at 在一次 run 之后逐行不变。
--
--  所以这两张表与 facts 层之间**没有外键**通向 contributions —— 它们只是
--  "输入"与"一次输入的运行记录"。定义是**输入**，不是结果。
--
--  ---------------------------------------------------------------------------
--  为什么 `pipeline_runs` 落盘，而导入请求（ImportRegistry）放内存
--  ---------------------------------------------------------------------------
--  导入请求内存化是对的：它是一个**进行中的东西**，进程没了就该没。run 不是
--  —— 它挂着的 job 是持久的，而"撤销这次批量跑"必须在**重启之后**还能按得
--  下去。一个重启就消失的撤销入口，恰好会在你最需要它的时候消失。
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 1. 管线定义（迁移 008 新增）
-- -----------------------------------------------------------------------------
CREATE TABLE pipeline_definitions (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    name            TEXT    NOT NULL,
    -- 规范形态的版本。3.7a 冻结 F3：**读不到版本是错误**，不是"当作 1"。
    -- 只读时升级、写时只写新版本，见 `core/pipeline.py` 的 `upgrade_definition`。
    format_version  INTEGER NOT NULL CHECK (format_version >= 1),
    -- canonical JSON（key 排序、无多余空白）—— `pipeline.canonical_definition`
    -- 是唯一的产出者。定义里**不含**运行期键（`_path` 之类）。
    definition      TEXT    NOT NULL,
    -- sha256(canonical JSON)。它与 `revision` 是**两个东西**，两个都留：
    --   * `revision` 是乐观锁（防两个编辑器互相覆盖）；
    --   * `definition_hash` 是**事实** —— "这次跑的是哪一版"。
    -- 只留 revision 的话，一个 run 说不出它跑的定义长什么样；只留 hash 的话，
    -- 保存冲突检测不了。
    definition_hash TEXT    NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    created_by      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    updated_by      TEXT    NOT NULL,
    -- 软删。**不提供 DELETE** —— 已经跑过的 run 靠 `pipeline_id` 指向它，
    -- 物理删除会让历史 run 指向一个不存在的定义。
    archived_at     TEXT,
    UNIQUE (project_id, name)
);

CREATE INDEX ix_pipeline_defs_project ON pipeline_definitions(project_id, archived_at);

-- -----------------------------------------------------------------------------
-- 2. 管线运行（一次"批量跑" = 1 行这里 + N 行 jobs）
-- -----------------------------------------------------------------------------
CREATE TABLE pipeline_runs (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    pipeline_id     INTEGER NOT NULL REFERENCES pipeline_definitions(id),
    -- 跑的是**哪一版**定义（不是"当前是哪一版"）—— 定义改过之后，
    -- 这次 run 仍然说得出它当时吃进去的是什么。
    definition_hash TEXT    NOT NULL,
    format_version  INTEGER NOT NULL,
    -- 应用状态，与 `jobs.applied` **同一个词、同一个语义**（3.7a 冻结 F12）：
    --   1 = 这一批的效果当前生效；0 = 已被 undo 撤销（可 redo）。
    -- `state` 说的是**执行**结果，`applied` 说的是**应用**状态 ——
    -- "跑完了但后来被撤销" 与 "执行失败" 是两件事（001 写过一遍）。
    applied         INTEGER NOT NULL DEFAULT 1 CHECK (applied IN (0, 1)),
    state           TEXT    NOT NULL DEFAULT 'running'
                    CHECK (state IN ('running','done','failed','partial','aborted')),
    -- 冗余列，但留着：作业历史页要在不解析 JSON 的前提下排序与显示
    -- "N 个步骤 / M 个作业"。与 `projects.scope_hash` 同一性质 —— 能被重算
    -- 出来，但每次重算的代价落在最热的那个查询上。
    step_count      INTEGER NOT NULL DEFAULT 0,
    job_count       INTEGER NOT NULL DEFAULT 0,
    -- 整份报告原样留一份。与 `ImportRecord.report` 同一条理由：ingest 的原话是
    -- "报告是唯一的交付通道"，API 层不另造一份。步骤 → job 的映射就活在
    -- `report.steps[].job_ids` 里，**不另开一张关联表**（少一张表就少一处
    -- 可以对不上）。唯一的例外是 `jobs.pipeline_run_id` —— 作业历史要按 run
    -- 分组，那是索引查询，不能靠 JSON 扫描。
    report          TEXT,
    actor           TEXT    NOT NULL DEFAULT 'gui',
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    UNIQUE (id, project_id)
);

CREATE INDEX ix_pipeline_runs_pipeline ON pipeline_runs(project_id, pipeline_id, id);

-- -----------------------------------------------------------------------------
-- 3. 作业归属：`jobs.pipeline_run_id`
-- -----------------------------------------------------------------------------
-- **不改 job 的粒度**（3.7a 冻结 F9）：一个文件一个 job，从 2.5 起就是数据层
-- 的纪律 —— `raw_files` 的 UNIQUE(project_id, sha256)、evidence 的 per-file
-- 归属、job_counts、explain_job_scope、GUI 的 can_undo/can_redo 全建立在它
-- 上面。为了"批量跑"把它改成一个 job，等于让这些东西同时换一套语义。
--
-- run 是**分组**，不是新的一种 job：`SELECT kind FROM jobs WHERE
-- pipeline_run_id = ?` 的结果必须全部是 'ingest'（3.7b 的 A4 断言）。
ALTER TABLE jobs ADD COLUMN pipeline_run_id INTEGER REFERENCES pipeline_runs(id);
CREATE INDEX ix_jobs_pipeline_run ON jobs(pipeline_run_id);

-- 跨项目兜底：挂在 A 项目的 run 上的 job，必须也是 A 项目的。
-- 与 v3.2 §2.4 的 Project Isolation 同一条：项目边界不能靠调用方记得。
CREATE TRIGGER trg_job_run_same_project
BEFORE UPDATE OF pipeline_run_id ON jobs
WHEN NEW.pipeline_run_id IS NOT NULL
 AND (SELECT project_id FROM pipeline_runs WHERE id = NEW.pipeline_run_id) <> NEW.project_id
BEGIN
    SELECT RAISE(ABORT, 'job 与 pipeline_run 必须同属一个项目（Project Isolation，v3.2 §2.4）。');
END;
