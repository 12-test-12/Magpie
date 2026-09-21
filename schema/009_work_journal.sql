-- 009_work_journal.sql —— 工作层写入的**撤销日志**（3.8）
--
-- 为什么需要一张新表:
--
-- `ops.undo` / `ops.redo` 的可逆性是建立在**事实层**上的：撤销一个 job = 给它的
-- 贡献追加 retract 抑制边（不删、不改，INV-01 无例外）。身份层与 inbox 各自的
-- 可逆性也各有自己的边（`revoked_by_job_id` / `closed_by_job_id`）。
--
-- 但「批量改状态」（`entities.lifecycle`）**不是事实层**：它是 D 工作层的人工
-- 状态，schema/001 的表头就写着「业务生命周期（人工/流程）」。它没有贡献可撤，
-- 也没有边可写 —— 撤销它需要的是一件这里从来没有过的东西：**改之前的那个值**。
--
-- 三个候选，为什么选第三个:
--
--   1. **把 lifecycle 做成一条 field 贡献**。否决：那会让 recompute 去投影它，
--      并让"工作层不可由事实重建"这条分层纪律（v3.2 §1 表）失效 —— 而 lifecycle
--      恰恰是**永远不该**被事实覆盖的列（导入不能把人工退休的资产复活）。
--   2. **把 before 值塞进 `activity_log.detail`**。否决：`activity_log` 是
--      append-only 的**审计**流，它的读者是"谁在什么时候改了什么"。让 undo 去
--      读它，等于把"审计记录"变成"撤销状态的唯一副本" —— 两者一旦分叉（清理、
--      归档、压缩），撤销就开始说假话，而那时没有任何东西会红。
--   3. **一张专门的日志表**（本文件）。它与 `tag_events` 是同一种东西：工作层
--      每一次写入留一行"之前是什么、之后是什么"，而且是**可被撤销消费**的那一份。
--
-- 形状说明:
--
--   * `before_json` / `after_json` 存 canonical JSON（标量也包成 JSON），
--     `NULL` 表示"该列当时是 NULL"。用 JSON 而不是 TEXT 是为了下一种工作层列
--     （`review_status` 之类）不必再改一次 schema。
--   * `UNIQUE (job_id, entity_id, field)` —— 同一个 job 对同一个实体的同一个
--     列只能有一条日志。批量操作里一个实体出现两次是调用方的错，让它当场炸，
--     而不是留下一份自相矛盾的日志（撤销时按 id 顺序回放，两条会互相盖掉）。
--   * 只有**非平凡**的变化才写（`before == after` 不写）。于是"撤销一个 job"
--     读到的每一条都一定改过东西，"重放"也一样。这条同时让 undo 的报告里
--     `reverted` 的计数是"真的被改回去的行数"，而不是"摸过的行数"。
--
-- **只新增、不改旧**（R4）: 本文件只建一张新表，不碰 001–008 的任何一行。
-- 已应用的迁移文件被改动 → 校验和不匹配 → `migrate` 直接报错。

CREATE TABLE work_journal (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    entity_id   INTEGER NOT NULL,
    -- 工作层的列名。今天只有 lifecycle（批量改状态）。以后再加工作层列时，
    -- 这里多一个取值即可 —— 表形状不必动。
    field       TEXT    NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    created_at  TEXT    NOT NULL,
    UNIQUE (job_id, entity_id, field),
    -- 跨项目兜底（与 008 的 trg_job_run_same_project 同一条纪律）:
    -- 一条挂在 A 项目 job 上的日志，指的必须是 A 项目的实体。
    -- FK 指向复合键 (id, project_id)，于是"跨项目引用"在数据库层就不可能。
    FOREIGN KEY (entity_id, project_id) REFERENCES entities(id, project_id)
);

CREATE INDEX ix_work_journal_job ON work_journal(job_id);
CREATE INDEX ix_work_journal_entity ON work_journal(project_id, entity_id, field);
