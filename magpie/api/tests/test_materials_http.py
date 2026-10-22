import time

from magpie.api.tests.test_routes import HEADERS, client, loaded  # noqa: F401


def test_notes_tasks_manual_fields_and_stale_http(loaded):
    def get(path,**kwargs): return loaded.get('/api/v1/'+path,headers=HEADERS,params={'project':'demo',**kwargs})
    def post(path,data): return loaded.post('/api/v1/'+path+'?project=demo',headers=HEADERS,json=data)
    target = get('workspace/browse',mode='list').json()['data']['rows'][0]
    evidence = get('workspace/browse',category='evidence',mode='list').json()['data']['rows'][0]
    created = post('workspace/notes',{'entity_id':target['id'],'text':'HTTP 笔记','evidence_ids':[evidence['id']]}).json()['data']
    nid = created['observation_id']
    note = get(f'workspace/notes/{nid}').json()['data']
    payload = {'text':'修订 %_','expected_version':note['version'],'reason':'补证据','evidence_ids':[evidence['id']]}
    path = f'/api/v1/workspace/notes/{nid}?project=demo'
    assert loaded.put(path,headers=HEADERS,json=payload).status_code == 200
    assert loaded.put(path,headers=HEADERS,json=payload).status_code == 409
    assert get('workspace/notes',evidence_id=evidence['id']).json()['data']['total'] == 1
    task = post('workspace/tasks',{'title':'查证据','evidence_ids':[evidence['id']]}).json()['data']
    assert get('workspace/tasks',evidence_id=evidence['id']).json()['data']['rows'][0]['id'] == task['id']
    patch = {'display_name':'HTTP 人工名称','review_status':'reviewed','expected_version':0,'reason':'标记'}
    url = f"/api/v1/entities/{target['id']}?project=demo"
    assert loaded.patch(url,headers=HEADERS,json=patch).status_code == 200
    assert loaded.patch(url,headers=HEADERS,json=patch).status_code == 409
    assert get(f"workspace/entities/{target['id']}/values").json()['data']['display_name'] == patch['display_name']


def test_search_diff_paste_and_js_staging_use_native_import(loaded):
    base = '/api/v1/'
    search = loaded.get(base+'search',headers=HEADERS,params={'project':'demo','q':'example'})
    assert search.status_code == 200 and search.json()['data']['groups']
    diff = loaded.get(base+'diff',headers=HEADERS,params={'project':'demo','limit':1})
    assert diff.status_code == 200 and len(diff.json()['data']['rows']) == 1
    text = "fetch('https://materials.example.test/' + 'orders');"
    response = loaded.post(base+'paste',headers=HEADERS,json={'project':'demo','text':text,'name':'original.js','resource_url':'https://materials.example.test/app.js'})
    assert response.status_code == 202, response.text
    record = loaded.app.state.ctx.imports.get(response.json()['data']['import_id'])
    deadline = time.monotonic()+15
    while not record.terminal and time.monotonic() < deadline: time.sleep(.02)
    assert record.terminal
    rows = loaded.get(base+'workspace/browse',headers=HEADERS,params={'project':'demo','category':'js','mode':'list','q':'materials'}).json()['data']['rows']
    assert len(rows) == 1
    details = loaded.get(base+f"workspace/entities/{rows[0]['id']}",headers=HEADERS,params={'project':'demo','section':'js'}).json()['data']
    assert any(row['semantic_field']=='js_candidate' for row in details['rows'])
    assert loaded.post(base+'input-stage',headers=HEADERS,json={'project':'demo','text':'x','name':'../x.js'}).status_code == 400
    for name in ('CON','NUL.txt','app.js.','a|b.js'):
        assert loaded.post(base+'input-stage',headers=HEADERS,json={'project':'demo','text':'x','name':name}).status_code == 400
    assert loaded.post(base+'paste',headers=HEADERS,json={'project':'demo','text':'x','resource_url':'file:///x.js'}).status_code == 400
    assert loaded.get(base+'diff',headers=HEADERS,params={'project':'demo','since':'oops'}).status_code == 400
