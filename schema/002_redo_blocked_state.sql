-- =============================================================================
--  002_redo_blocked_state.sql — 收窄 `undo_incomplete`（v3.2-r4 修正 #1）
--
--  001 的 `undo_incomplete` 规则是错的:
--      applied = 0  ⇒  该 job 的贡献必须全部被抑制
--  它把 `applied = 0` 读成了"这个 job 被完全撤销"。但 r4 给 `redo` 定的是
--  **部分成功**语义（core/ops.py `redo()` 的 blocked 分支）: 当一个 alias 已被
--  更新的身份决定占用时不覆盖它，而是恢复该 job 的全部贡献、保留那条撤销边、
--  `applied` 停在 0，并把 blocked 清单交人工处置。于是
--  "applied = 0 且存在活跃贡献"是一个**合法**状态。
--  实测后果: 任何真的写过贡献的 job 走 blocked 分支，`assert_integrity` 都会抛
--  InvariantError，redo 整笔回滚 —— 调用方看到的是 "integrity violated:
--  undo_incomplete"，而不是 blocked 报告。既有测试之所以通过，只是因为那个
--  rekey job 一条贡献都没有。
--
--  为什么不是删掉这条规则: 它仍要在真正管用的场景生效 ——
--  "撤销了一半"（applied=0、有活跃贡献、**且没有任何撤销边**）必须违规。
--  区分两者的钥匙就是撤销边: blocked redo 一定还留着边，而一个没做完的 undo
--  没有边可留。
--
--  实现说明: SQLite 没有 ALTER VIEW，改一条规则也只能 DROP + CREATE 整个视图。
--  因此本文件**完整重述** v_integrity_violations（其余规则与 001 逐字一致）。
--  将来再改视图请照此新开一个迁移，不要回头改本文件。
-- =============================================================================

DROP VIEW IF EXISTS v_integrity_violations;

CREATE VIEW v_integrity_violations AS
    -- INV-03: contribution 指向不存在的 subject（悬空）
    SELECT 'orphan_contribution' AS violation, CAST(c.id AS TEXT) AS ref, c.project_id
      FROM contributions c
     WHERE NOT EXISTS (
            SELECT 1 FROM entities      e WHERE c.subject_kind='entity'      AND e.id=c.subject_id
            UNION ALL
            SELECT 1 FROM observations  o WHERE c.subject_kind='observation' AND o.id=c.subject_id
            UNION ALL
            SELECT 1 FROM relations     r WHERE c.subject_kind='relation'    AND r.id=c.subject_id)
UNION ALL
    -- INV-06: contribution 与 subject 跨 project
    SELECT 'cross_project_contribution', CAST(c.id AS TEXT), c.project_id
      FROM contributions c
      JOIN entities e ON c.subject_kind='entity' AND e.id=c.subject_id
     WHERE e.project_id <> c.project_id
UNION ALL
    SELECT 'cross_project_contribution', CAST(c.id AS TEXT), c.project_id
      FROM contributions c
      JOIN observations o ON c.subject_kind='observation' AND o.id=c.subject_id
     WHERE o.project_id <> c.project_id
UNION ALL
    SELECT 'cross_project_contribution', CAST(c.id AS TEXT), c.project_id
      FROM contributions c
      JOIN relations r ON c.subject_kind='relation' AND r.id=c.subject_id
     WHERE r.project_id <> c.project_id
UNION ALL
    -- INV-15: supersession 指向不存在 / 被删的 contribution
    SELECT 'orphan_supersession', CAST(s.id AS TEXT), s.project_id
      FROM contribution_supersessions s
     WHERE NOT EXISTS (SELECT 1 FROM contributions c WHERE c.id = s.contribution_id)
UNION ALL
    -- INV-08: evidence 与 raw_file 跨 project
    SELECT 'cross_project_evidence', CAST(ev.id AS TEXT), ev.project_id
      FROM evidence ev JOIN raw_files rf ON rf.id = ev.raw_file_id
     WHERE rf.project_id <> ev.project_id
UNION ALL
    -- INV-07: contribution 与 evidence 跨 project
    SELECT 'cross_project_contribution_evidence', CAST(c.id AS TEXT), c.project_id
      FROM contributions c JOIN evidence ev ON ev.id = c.evidence_id
     WHERE ev.project_id <> c.project_id
UNION ALL
    -- 缓存一致性: is_current 有 active contribution 却为 0
    SELECT 'cache_understated_observation', CAST(o.id AS TEXT), o.project_id
      FROM observations o
     WHERE o.is_current = 0 AND EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='observation' AND c.subject_id=o.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    -- 缓存一致性: is_current = 1 却无 active contribution
    SELECT 'cache_overstated_observation', CAST(o.id AS TEXT), o.project_id
      FROM observations o
     WHERE o.is_current = 1 AND NOT EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='observation' AND c.subject_id=o.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    SELECT 'cache_understated_relation', CAST(r.id AS TEXT), r.project_id
      FROM relations r
     WHERE r.is_current = 0 AND EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='relation' AND c.subject_id=r.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    SELECT 'cache_overstated_relation', CAST(r.id AS TEXT), r.project_id
      FROM relations r
     WHERE r.is_current = 1 AND NOT EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='relation' AND c.subject_id=r.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    SELECT 'cache_understated_entity', CAST(e.id AS TEXT), e.project_id
      FROM entities e
     WHERE e.is_current = 0 AND EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='entity' AND c.subject_id=e.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    SELECT 'cache_overstated_entity', CAST(e.id AS TEXT), e.project_id
      FROM entities e
     WHERE e.is_current = 1 AND NOT EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='entity' AND c.subject_id=e.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    -- 命名冲突: 同一 project 内 canonical key 必须唯一（身份层解析结果）
    --   v3.2-r2: 必须按 project 分组；canonical_key 只在 project 内可比（原实现全库分组 → 多项目误报）
    SELECT 'duplicate_canonical_key', ii.canonical_key, ek.project_id
      FROM entity_identity ii
      JOIN entity_keys ek ON ek.entity_id = ii.canonical_entity_id
     WHERE ii.canonical_key IS NOT NULL
     GROUP BY ek.project_id, ii.canonical_key
    HAVING COUNT(DISTINCT ii.canonical_entity_id) > 1
UNION ALL
    -- INV-16: 事实里的主体归属/结构必须与缓存行一致
    SELECT 'observation_owner_mismatch', CAST(c.id AS TEXT), c.project_id
      FROM contributions c
      JOIN observations o ON o.id = c.subject_id
     WHERE c.subject_kind = 'observation'
       AND (o.entity_id <> c.owner_entity_id
         OR o.field_namespace <> c.field_namespace
         OR o.semantic_field <> c.semantic_field
         OR o.value <> c.field_value)
UNION ALL
    SELECT 'relation_owner_mismatch', CAST(c.id AS TEXT), c.project_id
      FROM contributions c
      JOIN relations r ON r.id = c.subject_id
     WHERE c.contribution_kind = 'relation'
       AND (r.src_id <> c.owner_entity_id
         OR r.rel <> c.relation_kind
         OR r.dst_id <> CAST(json_extract(c.field_value,'$.dst_id') AS INTEGER))
UNION ALL
    -- INV-17: evidence 的 locator 与 recovery_state 必须自洽
    SELECT 'evidence_locator_state_mismatch', CAST(e.id AS TEXT), e.project_id
      FROM evidence e
     WHERE (e.recovery_state = 'hash_only') <> (e.locator IS NULL)
UNION ALL
    -- 时间区间缓存一致性: observed_at / observed_at_min 必须等于活贡献区间的上/下界
    SELECT 'observed_at_interval_mismatch', CAST(o.id AS TEXT), o.project_id
      FROM observations o
     WHERE o.is_current = 1
       AND (o.observed_at     <> (SELECT MAX(c.observed_at)
                                    FROM v_active_contributions c
                                   WHERE c.subject_kind='observation' AND c.subject_id=o.id)
         OR o.observed_at_min <> (SELECT MIN(c.observed_at_min)
                                    FROM v_active_contributions c
                                   WHERE c.subject_kind='observation' AND c.subject_id=o.id))
UNION ALL
    -- INV-13(b) 幻影行: 缓存行不得"无法被任何贡献解释"
    --   observation / relation: 一条贡献都没有 → 只能由 vacuum 回收，不允许残留
    SELECT 'phantom_cache_row', 'observation:' || CAST(o.id AS TEXT), o.project_id
      FROM observations o
     WHERE NOT EXISTS (SELECT 1 FROM contributions c
                        WHERE c.subject_kind='observation' AND c.subject_id=o.id)
UNION ALL
    SELECT 'phantom_cache_row', 'relation:' || CAST(r.id AS TEXT), r.project_id
      FROM relations r
     WHERE NOT EXISTS (SELECT 1 FROM contributions c
                        WHERE c.subject_kind='relation' AND c.subject_id=r.id)
UNION ALL
    --   entity: 自己无贡献、却仍被其它缓存行引用 → 悬空解释（墓碑行必须由贡献解释）
    SELECT 'phantom_cache_row', 'entity:' || CAST(e.id AS TEXT), e.project_id
      FROM entities e
     WHERE NOT EXISTS (SELECT 1 FROM contributions c
                        WHERE c.subject_kind='entity' AND c.subject_id=e.id)
       AND (EXISTS (SELECT 1 FROM observations o WHERE o.entity_id=e.id)
         OR EXISTS (SELECT 1 FROM relations r WHERE r.src_id=e.id OR r.dst_id=e.id)
         OR EXISTS (SELECT 1 FROM entity_identity i
                     WHERE i.canonical_entity_id=e.id AND i.entity_id<>e.id))
UNION ALL
    -- 每个 entity_key 必须有**生效**的 alias（否则 key 无法解析）
    SELECT 'key_without_alias', ek.entity_key, ek.project_id
      FROM entity_keys ek
     WHERE NOT EXISTS (SELECT 1 FROM entity_alias a
                        WHERE a.project_id = ek.project_id
                          AND a.alias_key = ek.entity_key
                          AND a.revoked_by_job_id IS NULL)
UNION ALL
    -- v3.2-r4: 生命周期边必须与 job 的 applied 一致
    --   （撤销边存在 ⇔ 该 job 当前处于"未应用"状态）
    SELECT 'revocation_edge_inconsistent', 'alias:' || CAST(a.id AS TEXT), a.project_id
      FROM entity_alias a JOIN jobs j ON j.id = a.revoked_by_job_id
     WHERE a.revoked_by_job_id IS NOT NULL AND j.applied <> 0
UNION ALL
    SELECT 'revocation_edge_inconsistent', 'merge_member:' || CAST(m.entity_id AS TEXT),
           m.project_id
      FROM entity_merge_members m JOIN jobs j ON j.id = m.revoked_by_job_id
     WHERE m.revoked_by_job_id IS NOT NULL AND j.applied <> 0
UNION ALL
    SELECT 'revocation_edge_inconsistent', 'merge_event:' || CAST(ev.id AS TEXT), ev.project_id
      FROM entity_merge_events ev JOIN jobs j ON j.id = ev.unmerged_by_job_id
     WHERE ev.unmerged_by_job_id IS NOT NULL AND j.applied <> 0
UNION ALL
    SELECT 'revocation_edge_inconsistent', 'inbox_closed:' || CAST(i.id AS TEXT), i.project_id
      FROM inbox_items i JOIN jobs j ON j.id = i.closed_by_job_id
     WHERE i.closed_by_job_id IS NOT NULL AND j.applied <> 0
UNION ALL
    SELECT 'revocation_edge_inconsistent', 'inbox_opened:' || CAST(i.id AS TEXT), i.project_id
      FROM inbox_items i JOIN jobs j ON j.id = i.opened_by_undo_job_id
     WHERE i.opened_by_undo_job_id IS NOT NULL AND j.applied <> 0
UNION ALL
    -- v3.2-r4 修正（002）: 未应用的 job 必须真的把它的贡献全部变为非活跃。
    --   例外: **还留着撤销边**的 job —— 那是 `redo` 的部分成功状态
    --   （贡献已恢复为活跃，身份决定被人工作业占住，applied 停在 0；
    --    见 core/ops.py `redo()` 的 blocked 分支）。没有这条例外，
    --   任何"真的写过贡献"的 job 走 blocked 分支都会撞上本规则，
    --   于是整笔 redo 回滚、调用方拿到的是 integrity 报错而不是 blocked 报告。
    --   代价: 对有撤销边的 job，本规则不再能证明"undo 干完了"。
    SELECT 'undo_incomplete', 'job:' || CAST(j.id AS TEXT), j.project_id
      FROM jobs j
     WHERE j.applied = 0
       AND EXISTS (SELECT 1 FROM contributions c
                    WHERE c.job_id = j.id
                      AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                       WHERE s.contribution_id = c.id))
       AND NOT EXISTS (
             SELECT 1 FROM entity_alias         a  WHERE a.revoked_by_job_id     = j.id
             UNION ALL
             SELECT 1 FROM entity_merge_members m  WHERE m.revoked_by_job_id     = j.id
             UNION ALL
             SELECT 1 FROM entity_merge_events  ev WHERE ev.unmerged_by_job_id   = j.id
             UNION ALL
             SELECT 1 FROM inbox_items          i  WHERE i.closed_by_job_id      = j.id
             UNION ALL
             SELECT 1 FROM inbox_items          i2 WHERE i2.opened_by_undo_job_id = j.id)
UNION ALL
    -- compact 语义守恒: logical_job_count 缓存必须等于 job_count + compacted_job_count
    SELECT 'logical_job_count_mismatch', CAST(o.id AS TEXT), o.project_id
      FROM observations o
     WHERE o.logical_job_count <> (
            SELECT COALESCE(SUM(c.job_count + c.compacted_job_count), 0)
              FROM contributions c
             WHERE c.subject_kind='observation' AND c.subject_id=o.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    -- merge overlay 体检: merged_into 指向不存在的实体
    SELECT 'dangling_merged_into', CAST(e.id AS TEXT), e.project_id
      FROM entities e
     WHERE e.merged_into IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM entities t WHERE t.id = e.merged_into);
