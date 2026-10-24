/* All writes below are real UI actions against the launcher's synthetic project. */
const assert = require('node:assert/strict');
const path = require('node:path');

module.exports = async function researchSmoke({context,page,url,production,output,checks}) {
  const fixture = JSON.parse(process.env.MAGPIE_RESEARCH_FIXTURE || 'null');
  assert.ok(fixture?.project && fixture.note && fixture.new_evidence, 'Research fixture is required');
  const nav = (target,name) => target.getByLabel('项目导航').getByRole('button',{name:new RegExp(name)}).click();
  async function result(target,method,suffix,action,status=200) {
    const [response] = await Promise.all([target.waitForResponse(r=>r.request().method()===method && new URL(r.url()).pathname.endsWith(suffix)),action()]);
    assert.equal(response.status(),status);
    const payload = await response.json();
    if (status === 200) assert.equal(payload.ok,true);
    return status === 200 ? payload.data : payload.error;
  }
  async function setup(target,address) {
    await target.goto(address); await target.getByLabel('当前项目').selectOption(fixture.project);
  }
  async function openNote(target) {
    await nav(target,'研究笔记');
    await target.getByLabel('笔记归档筛选').selectOption('all');
    await target.getByRole('button',{name:'应用筛选',exact:true}).click();
    await target.locator('.workspace-table .row-link').click();
    await target.getByLabel('编辑笔记正文').waitFor();
  }
  async function searchOpen(target,keyword) {
    await target.locator('.topbar-search').click();
    await target.getByLabel('全局检索关键词').fill(keyword);
    await target.locator('.palette .search-hit').first().click();
  }
  const noteSave = (target,status=200) => result(target,'PUT','/workspace/notes/' + fixture.note,
    ()=>target.getByRole('button',{name:'保存笔记修订',exact:true}).click(),status);
  const manualSave = (target,status=200) => result(target,'PATCH','/entities/' + fixture.host,
    ()=>target.getByRole('button',{name:'保存人工字段',exact:true}).click(),status);
  const other = await context.newPage(); other.setDefaultTimeout(15000);
  try {
    await setup(page,url); await openNote(page);
    await setup(other,url); await openNote(other);
    await other.getByLabel('编辑笔记正文').fill('我的合成草稿');
    await other.getByLabel('笔记修改理由').fill('保留研究补充');
    await page.getByLabel('编辑笔记正文').fill('另一窗口新增线索');
    await page.getByLabel('归档笔记',{exact:true}).check();
    await page.getByLabel('搜索关联证据').fill(fixture.evidence_query);
    await page.locator('.note-editor .target-results button').first().click();
    await page.getByLabel('笔记修改理由').fill('归档并补充证据');
    assert.equal((await noteSave(page)).version,1);
    assert.equal((await noteSave(other,409)).code,'E_STALE_RESEARCH');
    assert.equal(await other.getByLabel('编辑笔记正文').inputValue(),'我的合成草稿');
    assert.equal(await other.getByRole('button',{name:'保存笔记修订',exact:true}).isDisabled(),true);
    await other.getByRole('button',{name:'核对最新值与草稿',exact:true}).click();
    await other.getByLabel('字段选择：笔记正文').waitFor();
    assert.equal(await other.getByRole('button',{name:'应用选择到草稿',exact:true}).isDisabled(),true);
    assert.equal(await other.getByLabel('字段选择：归档状态').inputValue(),'latest');
    assert.equal(await other.getByLabel('字段选择：关联研究证据').inputValue(),'latest');
    await other.setViewportSize({width:1000,height:850});
    assert.equal(await other.evaluate(()=>document.documentElement.scrollWidth),1000);
    await other.screenshot({path:path.join(output,'note-merge-narrow.jpg'),fullPage:true});
    await other.getByLabel('字段选择：笔记正文').selectOption('draft');
    let writes = 0; const countWrites = request => {if (request.method()==='PUT') writes++;};
    other.on('request',countWrites);
    await other.getByRole('button',{name:'应用选择到草稿',exact:true}).click();
    assert.equal(await other.getByLabel('归档笔记',{exact:true}).isChecked(),true);
    assert.equal(writes,0); other.off('request',countWrites);
    // A third update after comparison must still be rejected by the server.
    await page.getByLabel('编辑笔记正文').fill('另一窗口再次补充');
    await page.getByLabel('笔记修改理由').fill('并发更新仍保护');
    assert.equal((await noteSave(page)).version,2);
    assert.equal((await noteSave(other,409)).code,'E_STALE_RESEARCH');
    await other.getByRole('button',{name:'核对最新值与草稿',exact:true}).click();
    await other.getByLabel('字段选择：笔记正文').selectOption('draft');
    await other.getByRole('button',{name:'应用选择到草稿',exact:true}).click();
    const merged = await noteSave(other);
    assert.equal(merged.text,'我的合成草稿'); assert.equal(merged.archived,true);
    assert.deepEqual(merged.evidence_links.map(row=>row.id),[fixture.new_evidence]);
    assert.equal(merged.version,3);
    await other.getByRole('button',{name:'查看修订历史',exact:true}).click();
    await other.getByText('3 次修订',{exact:true}).waitFor();
    checks.push('note three-way choices preserve new archive/evidence, do not auto-write and reject another concurrent update');

    await searchOpen(page,'merge.example.test'); await searchOpen(other,'merge.example.test');
    for (const target of [page,other]) await target.locator('.manual-values summary').click();
    await other.getByLabel('人工名称',{exact:true}).fill('我的合成名称');
    await other.getByLabel('人工字段修改理由').fill('整理业务名称');
    await page.getByLabel('人工名称',{exact:true}).fill('另一窗口名称');
    await page.getByLabel('人工审核状态',{exact:true}).selectOption('reviewed');
    await page.getByLabel('人工字段修改理由').fill('人工复核');
    await manualSave(page); await manualSave(other,409);
    await other.getByRole('button',{name:'核对最新值与草稿',exact:true}).click();
    await other.getByLabel('字段选择：人工名称').selectOption('draft');
    await other.getByRole('button',{name:'应用选择到草稿',exact:true}).click();
    const manual = await manualSave(other);
    assert.equal(manual.manual_values.display_name,'我的合成名称');
    assert.equal(manual.manual_values.review_status,'reviewed');
    await other.screenshot({path:path.join(output,'manual-merge.jpg'),fullPage:true});
    checks.push('manual field conflict preserves another window review state');

    await searchOpen(page,fixture.evidence_query);
    await page.locator('.research-materials summary').click();
    await page.getByRole('button',{name:'＋ 为此资料创建研究任务',exact:true}).click();
    await page.getByLabel('任务标题').fill('持续回归：证据待办');
    const task = await result(page,'POST','/workspace/tasks',()=>page.getByRole('button',{name:'保存任务',exact:true}).click());
    assert.deepEqual(task.evidence_links.map(row=>row.id),[fixture.new_evidence]);
    await page.reload(); await page.getByLabel('当前项目').selectOption(fixture.project); await nav(page,'研究任务');
    await page.getByRole('button',{name:'持续回归：证据待办',exact:true}).click();
    await page.getByLabel('移除关联 ' + fixture.new_evidence).waitFor();
    checks.push('evidence-linked task persists across page reload');

    await page.locator('.workspace-topbar').getByRole('button',{name:'＋ 导入',exact:true}).click();
    await page.locator('.local-input summary').click();
    await page.getByLabel('选择本地文本或 JS 文件').setInputFiles(path.resolve('bench/e2e/research-materials/local.js'));
    await page.getByLabel('JS 资源绝对 URL').fill('https://merge.example.test/regression.js');
    await result(page,'POST','/input-stage',()=>page.getByRole('button',{name:'暂存到导入列表',exact:true}).click());
    await page.getByRole('button',{name:'开始导入',exact:true}).click();
    const report = page.locator('.report .rpt tbody tr');
    await report.waitFor();
    assert.equal(await report.locator('td').nth(1).innerText(),'done');
    const jobId = Number(await report.locator('td').nth(4).innerText());
    assert.ok(jobId > 0);
    await page.locator('.dialog').getByRole('button',{name:'关闭',exact:true}).click();
    await nav(page,'JS 资源');
    await page.locator('.group-card').click(); await page.locator('.group-card').click();
    await page.locator('.workspace-table .row-link').click();
    await page.getByText('静态代码引用候选',{exact:true}).first().waitFor();
    assert.match(await page.locator('.detail-content').innerText(),/未支持\/不确定调用/);
    await page.getByRole('button',{name:'来源证据',exact:true}).click();
    await page.locator('.detail-content .relation-list button').first().click();
    await page.locator('.evidence-preview').waitFor();
    assert.match(await page.locator('.evidence-preview').innerText(),/local\.js/);
    await page.screenshot({path:path.join(output,'local-js-evidence.jpg'),fullPage:true});
    checks.push('local JS file stages/imports through native queue and opens archived provenance');

    const diff = () => result(page,'GET','/diff',()=>nav(page,'增量变化'));
    const before = (await diff()).counts;
    assert.ok(before.facts > 0 && before.new > 0);
    await nav(page,'导入历史');
    const jobRow = page.locator('.jobs .rows tr').filter({has:page.locator('td:first-child').filter({hasText:new RegExp('^' + jobId + '$')})});
    await jobRow.click();
    await result(page,'POST','/jobs/' + jobId + '/undo',()=>page.getByRole('button',{name:'撤销',exact:true}).click());
    assert.deepEqual((await diff()).counts,{new:0,changed:0,facts:0});
    await nav(page,'导入历史'); await jobRow.click();
    await result(page,'POST','/jobs/' + jobId + '/redo',()=>page.getByRole('button',{name:'复原',exact:true}).click());
    assert.deepEqual((await diff()).counts,before);
    checks.push('JS job Undo/Redo changes effective contribution totals and restores the same counts');

    await setup(page,production); await openNote(page);
    assert.equal(await page.getByLabel('编辑笔记正文').inputValue(),'我的合成草稿');
    assert.equal(await page.getByLabel('归档笔记',{exact:true}).isChecked(),true);
    await page.getByRole('button',{name:'查看证据 #' + fixture.new_evidence + ' →',exact:true}).waitFor();
    await page.getByLabel('笔记修改理由').fill('正式静态页面验收');
    const saved = await noteSave(page); assert.equal(saved.version,4); assert.equal(saved.archived,true);
    await page.screenshot({path:path.join(output,'production-note.jpg'),fullPage:true});
    await page.setViewportSize({width:1000,height:850});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),1000);
    await page.screenshot({path:path.join(output,'production-note-narrow.jpg'),fullPage:true});
    await page.getByLabel('当前项目').selectOption('API 筛选空项目');
    await page.getByText('没有符合条件的结果',{exact:true}).waitFor();
    assert.equal(await page.locator('.note-editor').count(),0);
    checks.push('production note editor saves merged references, 1000px layout and empty-project isolation');
  } finally { await other.close(); }
};
