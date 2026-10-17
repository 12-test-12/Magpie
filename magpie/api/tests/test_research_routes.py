from magpie.api.tests.test_routes import HEADERS, client, loaded  # noqa: F401


def test_http_research_tasks_metadata_and_validation(loaded):
    base = '/api/v1/workspace'
    target = loaded.get(base + '/browse', headers=HEADERS,
                        params={'project':'demo','mode':'list'}).json()['data']['rows'][0]
    args = {'headers': HEADERS, 'params': {'project':'demo'}}
    task = loaded.post(base + '/tasks', **args, json={'title':'申请测试账号', 'entity_ids':[target['id']]}).json()['data']
    assert task['status'] == 'pending' and task['links'][0]['id'] == target['id']
    updated = loaded.put(base + f"/tasks/{task['id']}", **args,
                         json={'title':task['title'],'version':1,'status':'done','entity_ids':[target['id']]})
    assert updated.status_code == 200 and updated.json()['data']['version'] == 2
    stale = loaded.put(base + f"/tasks/{task['id']}", **args, json={'title':'旧数据','version':1})
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'E_STALE_RESEARCH'
    assert loaded.post(base + '/tasks', **args, json={'title':'bad','entity_ids':[99999]}).status_code == 400
    assert loaded.post(base + '/tasks', **args, json={'title':'bad','status':'running'}).status_code == 400
    assert loaded.post(base + '/tasks', **args, json={'title':' '}).status_code == 400
    page = loaded.get(base + '/tasks', headers=HEADERS, params={'project':'demo','status':'done'}).json()['data']
    assert page['total'] == 1
    meta_url = base + f"/entities/{target['id']}/research"
    assert loaded.get(meta_url, **args).json()['data']['version'] == 0
    saved = loaded.put(meta_url, **args, json={'version':0,'criticality':1,'add_tags':['payment']})
    assert saved.status_code == 200 and saved.json()['data']['tags'] == ['payment']
    assert loaded.put(meta_url, **args, json={'version':0,'criticality':2}).status_code == 409
    assert loaded.put(meta_url, **args, json={'version':1,'criticality':0}).status_code == 400
    rows = loaded.get(base + '/browse', headers=HEADERS,
                      params={'project':'demo','mode':'list','tag':'payment','criticality':1}).json()['data']
    assert rows['total'] == 1 and rows['rows'][0]['id'] == target['id']


def test_search_and_work_records_are_project_scoped_and_authenticated(loaded):
    loaded.post('/api/v1/projects', headers=HEADERS, json={'name':'empty','scope':{}})
    base = '/api/v1/workspace'
    target = loaded.get(base + '/browse', headers=HEADERS, params={'project':'demo','mode':'list'}).json()['data']['rows'][0]
    task = loaded.post(base + '/tasks', headers=HEADERS, params={'project':'demo'}, json={'title':'demo only'}).json()['data']
    for suffix in ['/search?q=example','/tasks',f"/tasks/{task['id']}",f"/entities/{target['id']}/research"]:
        assert loaded.get(base + suffix).status_code == 401
    assert loaded.get(base + f"/tasks/{task['id']}", headers=HEADERS, params={'project':'empty'}).status_code == 404
    assert loaded.put(base + f"/tasks/{task['id']}", headers=HEADERS, params={'project':'empty'}, json={'title':'bad','version':1}).status_code == 404
    assert loaded.post(base + '/tasks', headers=HEADERS, params={'project':'empty'}, json={'title':'bad','entity_ids':[target['id']]}).status_code == 400
    assert loaded.put(base + f"/entities/{target['id']}/research", headers=HEADERS, params={'project':'empty'}, json={'version':0}).status_code == 400
    result = loaded.get(base + '/search', headers=HEADERS, params={'project':'demo','q':'example','limit':1}).json()['data']
    assert len(result['groups']) == 5 and all(len(g['rows']) <= 1 for g in result['groups'])
    empty = loaded.get(base + '/search', headers=HEADERS, params={'project':'empty','q':'example'}).json()['data']
    assert all(g['total'] == 0 for g in empty['groups'])
    for params in [{'q':''},{'q':'x','limit':11},{'q':'x'*301}]:
        assert loaded.get(base + '/search', headers=HEADERS, params={'project':'demo',**params}).status_code == 400
