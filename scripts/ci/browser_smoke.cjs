/* Real UI smoke against synthetic local data. No browser/tool installation here. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(path.join(process.env.MAGPIE_PLAYWRIGHT_ROOT || '', 'playwright'));
const url = process.env.MAGPIE_BROWSER_URL;
const production = process.env.MAGPIE_PRODUCTION_URL;
const output = process.env.MAGPIE_BROWSER_OUTPUT;
const token = new URL(url).searchParams.get('token');
const redact = text => String(text).split(token).join('[test-token]');

(async () => {
  for (const target of [url,production]) {
    const parsed = new URL(target);
    assert.equal(parsed.protocol,'http:'); assert.equal(parsed.hostname,'127.0.0.1');
  }
  const browser = await chromium.launch({headless:process.env.MAGPIE_BROWSER_HEADLESS !== '0',
    ...(process.env.MAGPIE_BROWSER_EXECUTABLE ? {executablePath:process.env.MAGPIE_BROWSER_EXECUTABLE} : {}),
    args:['--disable-background-networking']});
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const errors = [], external = [], checks = [];
  await context.route('**/*', route => {
    const requested = new URL(route.request().url());
    if (['http:','https:'].includes(requested.protocol) && requested.hostname !== '127.0.0.1') {
      external.push(requested.origin); return route.abort();
    }
    return route.continue();
  });
  context.on('page',target => target.on('pageerror',error => errors.push(redact(error))));
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  const browse = async action => {
    const [response] = await Promise.all([page.waitForResponse(r=>r.url().includes('/workspace/browse?') && r.request().method()==='GET'),action()]);
    assert.equal(response.status(),200); return (await response.json()).data;
  };
  const expand = async () => {if (!await page.locator('.api-research-filters').evaluate(el=>el.open)) await page.locator('.api-research-filters summary').click();};
  const apply = async () => {
    const fields = {parameter_name:'参数名筛选',parameter_location:'参数位置筛选',response_status:'响应状态码筛选',response_mime:'响应类型筛选',credential:'凭据线索筛选',api_origin:'来源性质筛选'};
    const expected = {};
    for (const [key,label] of Object.entries(fields)) expected[key] = await page.getByLabel(label,{exact:true}).inputValue();
    const [response] = await Promise.all([page.waitForResponse(r=>{
      if (!r.url().includes('/workspace/browse?')) return false;
      const query = new URL(r.url()).searchParams;
      return Object.entries(expected).every(([key,value])=>(query.get(key)||'')===value);
    }),page.getByRole('button',{name:'应用筛选',exact:true}).click()]);
    assert.equal(response.status(),200); return (await response.json()).data;
  };
  try {
    // dev.py prints its URL before Vite and the API finish binding their ports.
    // Retry connection startup only; an actual HTTP error remains a failure.
    for (const target of [production,url]) {
      const deadline = Date.now() + 30000;
      for (;;) {
        try {
          const response = await context.request.get(target,{timeout:2000,maxRedirects:0});
          assert.equal(response.status(),200);
          await response.dispose();
          break;
        } catch (error) {
          if (Date.now() >= deadline || !/ECONNREFUSED|ECONNRESET|ERR_CONNECTION_REFUSED|Timeout.*exceeded/i.test(String(error))) throw error;
          await new Promise(resolve=>setTimeout(resolve,500));
        }
      }
    }
    await page.goto(url);
    await page.getByLabel('当前项目').selectOption('API 研究筛选 · 合成验收');
    await browse(()=>page.getByLabel('项目导航').getByRole('button',{name:'API',exact:false}).click());
    await expand();
    await page.getByLabel('参数名筛选',{exact:true}).fill('redirect');
    await page.getByLabel('参数位置筛选',{exact:true}).selectOption('query');
    await page.getByLabel('响应状态码筛选',{exact:true}).fill('200');
    await page.getByLabel('响应类型筛选',{exact:true}).fill('application/json');
    await page.getByLabel('凭据线索筛选',{exact:true}).selectOption('cookie');
    await page.getByLabel('来源性质筛选',{exact:true}).selectOption('http_sample');
    assert.equal((await apply()).entity_total,1);
    await page.screenshot({path:path.join(output,'filters.jpg'),fullPage:true});
    assert.equal((await browse(()=>page.locator('.group-card').click())).entity_total,1);
    assert.equal((await browse(()=>page.locator('.group-card').click())).total,1);
    await page.locator('.workspace-table .row-link').click();
    await page.waitForFunction(()=>document.querySelectorAll('.sample-choices button').length===20);
    await page.locator('.sample-choices button').first().click();
    await Promise.all([page.waitForResponse(r=>r.url().includes('/workspace/entities/') && r.url().includes('cursor=')),
      page.locator('.workspace-detail .detail-pagination').getByRole('button',{name:'下一页',exact:true}).click()]);
    await page.waitForFunction(()=>document.querySelectorAll('.sample-choices button').length===4);
    assert.match(await page.locator('.selected-samples').innerText(),/样本/);
    await page.locator('.sample-choices button').first().click();
    await page.locator('.comparison-scroll').scrollIntoViewIfNeeded();
    assert.match(await page.locator('.comparison-scroll').innerText(),/摘要不同/);
    await page.screenshot({path:path.join(output,'comparison.jpg'),fullPage:true});
    await page.locator('.selected-samples').getByRole('button',{name:/打开样本.*证据/}).first().click();
    await page.locator('.evidence-preview').waitFor();
    assert.match(await page.locator('.evidence-preview').innerText(),/有效贡献链/);
    checks.push('same-request filtering, grouped counts, cross-page sample comparison and evidence');
    await page.getByLabel('关闭详情').click(); await expand();
    await page.getByLabel('凭据线索筛选',{exact:true}).selectOption('bearer');
    assert.equal((await apply()).entity_total,0);
    checks.push('200 + Bearer + redirect cannot borrow fields from separate requests');
    await page.getByLabel('当前项目').selectOption('API 筛选空项目');
    await page.getByText('没有符合条件的结果',{exact:true}).waitFor();
    checks.push('empty project has no cross-project results');
    await page.getByLabel('当前项目').selectOption('API 研究筛选 · 合成验收');
    await page.getByLabel('来源性质筛选',{exact:true}).waitFor({state:'attached'});
    await browse(()=>page.getByRole('button',{name:'重置',exact:true}).click());
    await page.setViewportSize({width:1000,height:850}); await expand();
    await page.getByLabel('参数名筛选',{exact:true}).fill('key_%');
    assert.equal((await apply()).entity_total,1);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),1000);
    await page.screenshot({path:path.join(output,'narrow.jpg'),fullPage:true});
    checks.push('literal percent/underscore parameter, collapsible filters and 1000px layout');
    await page.setViewportSize({width:1440,height:1000});
    await page.goto(production);
    await page.getByLabel('当前项目').selectOption('API 研究筛选 · 合成验收');
    await browse(()=>page.getByLabel('项目导航').getByRole('button',{name:'API',exact:false}).click());
    await expand(); await page.getByLabel('参数名筛选',{exact:true}).fill('redirect');
    await page.getByLabel('响应状态码筛选',{exact:true}).fill('200');
    await page.getByLabel('凭据线索筛选',{exact:true}).selectOption('cookie');
    assert.equal((await apply()).entity_total,1);
    checks.push('backend-served production assets execute the same API filter');
    await require('./research_smoke.cjs')({context,page,url,production,output,checks});
    assert.deepEqual(errors,[]); assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({status:'passed',checks,pageerrors:errors,external_requests:external},null,2));
    console.log(JSON.stringify({checks,pageerrors:errors,external_requests:external}));
  } catch (error) {
    await page.screenshot({path:path.join(output,'failure.jpg'),fullPage:true}).catch(()=>{});
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({status:'failed',checks,error:redact(error.stack),pageerrors:errors,external_requests:external},null,2));
    throw error;
  } finally {await browser.close();}
})().catch(error=>{console.error(redact(error.stack));process.exitCode=1;});
